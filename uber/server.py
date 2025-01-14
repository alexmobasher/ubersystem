import json
import mimetypes
import os
import traceback
from pprint import pformat

import cherrypy
import sentry_sdk
import jinja2
from cherrypy import HTTPError
from pockets import is_listy
from pockets.autolog import log
from sideboard.jsonrpc import json_handler, ERR_INVALID_RPC, ERR_MISSING_FUNC, ERR_INVALID_PARAMS, \
    ERR_FUNC_EXCEPTION, ERR_INVALID_JSON
from sideboard.websockets import trigger_delayed_notifications

from uber.config import c, Config
from uber.decorators import all_renderable, render
from uber.errors import HTTPRedirect
from uber.utils import mount_site_sections, static_overrides


mimetypes.init()

if c.SENTRY['enabled']:
    sentry_sdk.init(
        dsn=c.SENTRY['dsn'],
        environment=c.SENTRY['environment'],
        release=c.SENTRY['release'],

        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for performance monitoring.
        # We recommend adjusting this value in production.
        traces_sample_rate=c.SENTRY['sample_rate'] / 100
    )

def sentry_start_transaction():
    cherrypy.request.sentry_transaction = sentry_sdk.start_transaction(
        name=f"{cherrypy.request.method} {cherrypy.request.path_info}",
        op=f"{cherrypy.request.method} {cherrypy.request.path_info}",
    )
    cherrypy.request.sentry_transaction.__enter__()
cherrypy.tools.sentry_start_transaction = cherrypy.Tool('on_start_resource', sentry_start_transaction)

def sentry_end_transaction():
    cherrypy.request.sentry_transaction.__exit__(None, None, None)
cherrypy.tools.sentry_end_transaction = cherrypy.Tool('on_end_request', sentry_end_transaction)

@cherrypy.tools.register('before_finalize', priority=60)
def secureheaders():
    headers = cherrypy.response.headers
    hsts_header = 'max-age=' + str(c.HSTS['max_age'])
    if c.HSTS['include_subdomains']:
        hsts_header += '; includeSubDomains'
    if c.HSTS['preload']:
        if c.HSTS['max_age'] < 31536000:
            log.error('HSTS only supports preloading if max-age > 31536000')
        elif not c.HSTS['include_subdomains']:
            log.error('HSTS only supports preloading if subdomains are included')
        else:
            hsts_header += '; preload'
    headers['Strict-Transport-Security'] = hsts_header

def _add_email():
    [body] = cherrypy.response.body
    body = body.replace(b'<body>', (
        b'<body>Please contact us via <a href="CONTACT_URL">CONTACT_URL</a> if you\'re not sure why '
        b'you\'re seeing this page.').replace(b'CONTACT_URL', c.CONTACT_URL.encode('utf-8')))
    cherrypy.response.headers['Content-Length'] = len(body)
    cherrypy.response.body = [body]


cherrypy.tools.add_email_to_error_page = cherrypy.Tool('after_error_response', _add_email)


def get_verbose_request_context():
    """
    Return a string with lots of information about the current cherrypy request such as
    request headers, session params, and page location.

    Returns:

    """
    from uber.models.admin import AdminAccount

    page_location = 'Request: ' + cherrypy.request.request_line

    admin_name = AdminAccount.admin_name()
    admin_txt = 'Current admin user is: {}'.format(admin_name if admin_name else '[non-admin user]')

    max_reporting_length = 1000   # truncate to reasonably large size in case they uploaded attachments

    p = ["  %s: %s" % (k, str(v)[:max_reporting_length]) for k, v in cherrypy.request.params.items()]
    post_txt = 'Request Params:\n' + '\n'.join(p)

    session_txt = ''
    if hasattr(cherrypy, 'session'):
        session_txt = 'Session Params:\n' + pformat(cherrypy.session.items(), width=40)

    h = ["  %s: %s" % (k, v) for k, v in cherrypy.request.header_list]
    headers_txt = 'Request Headers:\n' + '\n'.join(h)

    return '\n'.join([page_location, admin_txt, post_txt, session_txt, headers_txt])


def log_with_verbose_context(msg, exc_info=False):
    full_msg = '\n'.join([msg, get_verbose_request_context()])
    log.error(full_msg, exc_info=exc_info)


def log_exception_with_verbose_context(debug=False, msg=''):
    """
    Write the request headers, session params, page location, and the last error's traceback to the cherrypy error log.
    Do this all one line so all the information can be collected by external log collectors and easily displayed.

    Debug param is there to play nice with the cherrypy logger
    """
    log_with_verbose_context('\n'.join([msg, 'Exception encountered']), exc_info=True)


def redirect_site_section(original, redirect, new_page='', *path, **params):
    path = cherrypy.request.path_info.replace(original, redirect)
    if new_page:
        path = path.replace(c.PAGE, new_page)
    if cherrypy.request.query_string:
        path += '?' + cherrypy.request.query_string
    raise HTTPRedirect(path)


cherrypy.tools.custom_verbose_logger = cherrypy.Tool('before_error_response', log_exception_with_verbose_context)


class StaticViews:
    @classmethod
    def path_args_to_string(cls, path_args):
        return '/'.join(path_args)

    @classmethod
    def get_full_path_from_path_args(cls, path_args):
        return 'static_views/' + cls.path_args_to_string(path_args)

    @classmethod
    def get_filename_from_path_args(cls, path_args):
        return path_args[-1]

    @classmethod
    def raise_not_found(cls, path, e=None):
        raise cherrypy.HTTPError(404, "The path '{}' was not found.".format(path)) from e

    @cherrypy.expose
    def index(self):
        self.raise_not_found('static_views/')

    @cherrypy.expose
    def default(self, *path_args, **kwargs):
        content_filename = self.get_filename_from_path_args(path_args)

        template_name = self.get_full_path_from_path_args(path_args)
        try:
            content = render(template_name)
        except jinja2.exceptions.TemplateNotFound as e:
            self.raise_not_found(template_name, e)

        guessed_content_type = mimetypes.guess_type(content_filename)[0]
        return cherrypy.lib.static.serve_fileobj(content, name=content_filename, content_type=guessed_content_type)


class AngularJavascript:
    @cherrypy.expose
    def magfest_js(self):
        """
        We have several Angular apps which need to be able to access our constants like c.ATTENDEE_BADGE and such.
        We also need those apps to be able to make HTTP requests with CSRF tokens, so we set that default.
        """
        cherrypy.response.headers['Content-Type'] = 'text/javascript'

        consts = {}
        for attr in dir(c):
            try:
                consts[attr] = getattr(c, attr, None)
            except Exception:
                pass

        js_consts = json.dumps({k: v for k, v in consts.items() if isinstance(v, (bool, int, str))}, indent=4)
        return '\n'.join([
            'angular.module("magfest", [])',
            '.constant("c", {})'.format(js_consts),
            '.constant("magconsts", {})'.format(js_consts),
            '.run(function ($http) {',
            '   $http.defaults.headers.common["CSRF-Token"] = "{}";'.format(c.CSRF_TOKEN),
            '});'
        ])

    @cherrypy.expose
    def static_magfest_js(self):
        """
        We have several Angular apps which need to be able to access our constants like c.ATTENDEE_BADGE and such.
        We also need those apps to be able to make HTTP requests with CSRF tokens, so we set that default.

        The static_magfest_js() version of magfest_js() omits any config
        properties that generate database queries.
        """
        cherrypy.response.headers['Content-Type'] = 'text/javascript'

        consts = {}
        for attr in dir(c):
            try:
                prop = getattr(Config, attr, None)
                if prop:
                    fget = getattr(prop, 'fget', None)
                    if fget and getattr(fget, '_dynamic', None):
                        continue
                consts[attr] = getattr(c, attr, None)
            except Exception:
                pass

        js_consts = json.dumps({k: v for k, v in consts.items() if isinstance(v, (bool, int, str))}, indent=4)
        return '\n'.join([
            'angular.module("magfest", [])',
            '.constant("c", {})'.format(js_consts),
            '.constant("magconsts", {})'.format(js_consts),
            '.run(function ($http) {',
            '   $http.defaults.headers.common["CSRF-Token"] = "{}";'.format(c.CSRF_TOKEN),
            '});'
        ])


@all_renderable(public=True)
class Root:
    def index(self):
        raise HTTPRedirect('landing/')

    def uber(self, *path, **params):
        """
        Some old browsers bookmark all urls as starting with /uber but Nginx
        automatically prepends this.  For backwards-compatibility, if someone
        comes to a url that starts with /uber then we redirect them to the
        same URL with that bit stripped out.

        For example, old laptops which have
            https://onsite.uber.magfest.org/uber/registration/register
        bookmarked as their homepage will automatically get redirected to
            https://onsite.uber.magfest.org/registration/register
        and so forth.
        """
        path = cherrypy.request.path_info[len('/uber'):]
        if cherrypy.request.query_string:
            path += '?' + cherrypy.request.query_string
        raise HTTPRedirect(path)

    static_views = StaticViews()
    angular = AngularJavascript()


mount_site_sections(c.MODULE_ROOT)


def error_page_404(status, message, traceback, version):
    return "Sorry, page not found!<br/><br/>{}<br/>{}".format(status, message)


c.APPCONF['/']['error_page.404'] = error_page_404

cherrypy.tree.mount(Root(), c.CHERRYPY_MOUNT_PATH, c.APPCONF)
static_overrides(os.path.join(c.MODULE_ROOT, 'static'))


def _make_jsonrpc_handler(services, debug=c.DEV_BOX, precall=lambda body: None):

    @cherrypy.expose
    @cherrypy.tools.force_json_in()
    @cherrypy.tools.json_out(handler=json_handler)
    def _jsonrpc_handler(self=None):
        id = None

        def error(status, code, message):
            response = {'jsonrpc': '2.0', 'id': id, 'error': {'code': code, 'message': message}}
            log.debug('Returning error message: {}', repr(response).encode('utf-8'))
            cherrypy.response.status = status
            return response

        def success(result):
            response = {'jsonrpc': '2.0', 'id': id, 'result': result}
            log.debug('Returning success message: {}', {
                'jsonrpc': '2.0', 'id': id, 'result': len(result) if is_listy(result) else str(result).encode('utf-8')})
            cherrypy.response.status = 200
            return response

        request_body = cherrypy.request.json
        if not isinstance(request_body, dict):
            return error(400, ERR_INVALID_JSON, 'Invalid json input: {!r}'.format(request_body))

        log.debug('jsonrpc request body: {}', repr(request_body).encode('utf-8'))

        id, params = request_body.get('id'), request_body.get('params', [])
        if 'method' not in request_body:
            return error(400, ERR_INVALID_RPC, '"method" field required for jsonrpc request')

        method = request_body['method']
        if method.count('.') != 1:
            return error(404, ERR_MISSING_FUNC, 'Invalid method ' + method)

        module, function = method.split('.')
        if module not in services:
            return error(404, ERR_MISSING_FUNC, 'No module ' + module)

        service = services[module]
        if not hasattr(service, function):
            return error(404, ERR_MISSING_FUNC, 'No function ' + method)

        if not isinstance(params, (list, dict)):
            return error(400, ERR_INVALID_PARAMS, 'Invalid parameter list: {!r}'.format(params))

        args, kwargs = (params, {}) if isinstance(params, list) else ([], params)

        precall(request_body)
        try:
            return success(getattr(service, function)(*args, **kwargs))
        except HTTPError as http_error:
            return error(http_error.code, ERR_FUNC_EXCEPTION, http_error._message)
        except Exception as e:
            log.error('Unexpected error', exc_info=True)
            message = 'Unexpected error: {}'.format(e)
            if debug:
                message += '\n' + traceback.format_exc()
            return error(500, ERR_FUNC_EXCEPTION, message)
        finally:
            trigger_delayed_notifications()

    return _jsonrpc_handler


jsonrpc_services = {}


def register_jsonrpc(service, name=None):
    name = name or service.__name__
    assert name not in jsonrpc_services, '{} has already been registered'.format(name)
    jsonrpc_services[name] = service


jsonrpc_app = _make_jsonrpc_handler(jsonrpc_services)
cherrypy.tree.mount(jsonrpc_app, c.CHERRYPY_MOUNT_PATH + '/jsonrpc', c.APPCONF)
