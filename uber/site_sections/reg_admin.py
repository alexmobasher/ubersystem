from itertools import chain
from uber.models.attendee import AttendeeAccount

import cherrypy
from pockets import groupify, listify
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import joinedload, raiseload, subqueryload
from sqlalchemy.orm.exc import NoResultFound

from uber.config import c, _config
from uber.custom_tags import pluralize
from uber.decorators import ajax, all_renderable, csv_file, not_site_mappable, site_mappable
from uber.errors import HTTPRedirect
from uber.models import ApiJob, Attendee, Department, DeptMembership, DeptMembershipRequest, Group
from uber.site_sections import devtools
from uber.utils import get_api_service_from_server, normalize_email


@all_renderable()
class Root:
    def receipt_items(self, session, id, message=''):
        try:
            model = session.attendee(id)
        except NoResultFound:
            model = session.group(id)

        return {
            'attendee': model if isinstance(model, Attendee) else None,
            'group': model,
            'message': message,
            'stripe_txn_opts': [(txn.stripe_transaction.id, txn.stripe_transaction.stripe_id)
                                for txn in model.stripe_txn_share_logs],
        }

    def add_receipt_item(self, session, id='', **params):
        try:
            model = session.attendee(id)
        except NoResultFound:
            model = session.group(id)
            
        stripe_txn_id = params.get('stripe_txn_id', '')
        if stripe_txn_id:
            stripe_txn = session.stripe_transaction(stripe_txn_id)

        session.add(session.create_receipt_item(
            model, float(params.get('amount')) * 100, params.get('desc'), stripe_txn if stripe_txn_id else None,
            params.get('txn_type')))

        item_type = "Payment" if params.get('txn_type') == c.PAYMENT else "Refund"

        raise HTTPRedirect('receipt_items?id={}&message={}', model.id, "{} added".format(item_type))

    def remove_receipt_item(self, session, id='', **params):
        item = session.receipt_item(id)
        item_type = "Payment" if item.txn_type == c.PAYMENT else "Refund"
        
        attendee_or_group = item.attendee if item.attendee_id else item.group
        session.delete(item)

        raise HTTPRedirect('receipt_items?id={}&message={}', attendee_or_group.id, "{} deleted".format(item_type))
    
    def add_refund_item(self, session, id='', **params):
        try:
            model = session.attendee(id)
        except NoResultFound:
            model = session.group(id)
        
        if params.get('item_name') and params.get('item_val'):
            model.refunded_items[params.get('item_name')] = params.get('item_val')
            session.add(model)
            message = "Item marked as refunded"
        else:
            message = "ERROR: Item not found"
        
        raise HTTPRedirect('receipt_items?id={}&message={}', model.id, message)
    
    def remove_refund_item(self, session, id='', **params):
        try:
            model = session.attendee(id)
        except NoResultFound:
            model = session.group(id)
        
        if params.get('item_name') and params.get('item_val'):
            del model.refunded_items[params.get('item_name')]
            session.add(model)
            message = "Refunded item removed"
        else:
            message = "ERROR: Refund item not found"
        
        raise HTTPRedirect('receipt_items?id={}&message={}', model.id, message)

    @not_site_mappable
    def remove_promo_code(self, session, id=''):
        attendee = session.attendee(id)
        attendee.paid = c.NOT_PAID
        attendee.promo_code = None
        attendee.badge_status = c.NEW_STATUS
        raise HTTPRedirect('../registration/form?id={}&message={}', id, "Promo code removed.")

    def attendee_accounts(self, session, message=''):
        return {
            'message': message,
            'accounts': session.query(AttendeeAccount).options(joinedload(AttendeeAccount.attendees), raiseload('*')).all(),
        }

    def delete_attendee_account(self, session, id, message='', **params):
        account = session.attendee_account(id)
        if not account:
            message = "No account found!"
        else:
            session.delete(account)
        raise HTTPRedirect('attendee_accounts?message={}', message or 'Account deleted.')

    @site_mappable
    def orphaned_attendees(self, session, message='', **params):
        from uber.site_sections.preregistration import set_up_new_account

        if cherrypy.request.method == 'POST':
            if 'account_email' not in params:
                message = "Please enter an account email to assign to this attendee."
            else:
                account_email = params.get('account_email').strip()
                attendee = session.attendee(params.get('id'))
                account = session.query(AttendeeAccount).filter_by(normalized_email=normalize_email(account_email)).first()
                if not attendee:
                    message = "Attendee not found!"
                elif not account:
                    set_up_new_account(session, attendee, account_email)
                    session.commit()
                    message = "New account made for {} under email {}.".format(attendee.full_name, account_email)
                else:
                    session.add_attendee_to_account(session.attendee(params.get('id')), account)
                    session.commit()
                    message = "{} is now being managed by account {}.".format(attendee.full_name, account_email)

        return {
            'message': message,
            'attendees': session.query(Attendee).filter(~Attendee.managers.any()).options(raiseload('*')).all(),
        }

    def payment_pending_attendees(self, session):
        possibles = session.possible_match_list()
        attendees = []
        pending = session.query(Attendee).filter_by(paid=c.PENDING).filter(Attendee.badge_status != c.INVALID_STATUS)
        for attendee in pending:
            attendees.append([attendee, set(possibles[attendee.email.lower()] + 
                                            possibles[attendee.first_name, attendee.last_name])])
        return {
            'attendees': attendees,
        }
    
    @ajax
    def invalidate_badge(self, session, id):
        attendee = session.attendee(id)
        attendee.badge_status = c.INVALID_STATUS
        session.add(attendee)

        session.commit()

        return {'invalidated': id}

    def attendees_who_owe_money(self, session):
        unpaid_attendees = [attendee for attendee in session.attendees_with_badges() 
                            if attendee.total_purchased_cost > attendee.amount_paid]
        return {
            'attendees': unpaid_attendees,
        }

    @csv_file
    @not_site_mappable
    def attendee_search_export(self, out, session, search_text='', order='last_first', invalid=''):
        filter = Attendee.badge_status.in_([c.NEW_STATUS, c.COMPLETED_STATUS, c.WATCHED_STATUS]) if not invalid else None
        
        search_text = search_text.strip()
        if search_text:
            attendees, error = session.search(search_text) if invalid else session.search(search_text, filter)

        if error:
            raise HTTPRedirect('../registration/index?search_text={}&order={}&invalid={}&message={}'
                              ).format(search_text, order, invalid, error)
        attendees = attendees.order(order)

        rows = devtools.prepare_model_export(Attendee, filtered_models=attendees)
        for row in rows:
            out.writerow(row)

    def attendee_account_form(self, session, id, message=''):
        account = session.attendee_account(id)

        return {
            'message': message,
            'account': account,
        }

    @site_mappable
    def import_attendees(self, session, target_server='', api_token='', query='', message='', which_import='', **params):
        service, service_message, target_url = get_api_service_from_server(target_server, api_token)
        message = message or service_message

        attendees, existing_attendees, results = {}, {}, {}
        accounts, existing_accounts = {}, {}
        groups, existing_groups = {}, {}
        results_name, href_base = '', ''

        if service and which_import:
            try:
                if which_import == 'attendees':
                    results = service.attendee.export(query=query)
                    results_name = 'attendees'
                    href_base = '{}/reg_admin/attendee_account_form?id={}'
                elif which_import == 'accounts':
                    results = service.attendee_account.export(query=query, all=params.get('all', False))
                    results_name = 'accounts'
                    href_base = '{}/registration/form?id={}'
                elif which_import == 'groups':
                    if params.get('dealers', ''):
                        results = service.group.dealers(status=params.get('status', None))
                    else:
                        results = service.group.export(query=query)
                    results_name = 'groups'
                    href_base = '{}/group_admin/form?id={}'

            except Exception as ex:
                message = str(ex)

        if cherrypy.request.method == 'POST' and not message:
            models = results.get(results_name, [])
            for model in models:
                model['href'] = href_base.format(target_url, model['id'])

            if models and which_import == 'attendees':
                attendees = models
                attendees_by_name_email = groupify(attendees, lambda a: (
                    a['first_name'].lower(),
                    a['last_name'].lower(),
                    normalize_email(a['email']),
                ))

                filters = [
                    and_(
                        func.lower(Attendee.first_name) == first,
                        func.lower(Attendee.last_name) == last,
                        Attendee.normalized_email == email,
                    )
                    for first, last, email in attendees_by_name_email.keys()
                ]

                existing_attendees = session.query(Attendee).filter(or_(*filters)).all()
                for attendee in existing_attendees:
                    existing_key = (attendee.first_name.lower(), attendee.last_name.lower(), attendee.normalized_email)
                    attendees_by_name_email.pop(existing_key, {})
                attendees = list(chain(*attendees_by_name_email.values()))

            if models and which_import == 'accounts':
                accounts = models
                accounts_by_email = groupify(accounts, lambda a: normalize_email(a['email']))

                existing_accounts = session.query(AttendeeAccount).filter(
                    AttendeeAccount.normalized_email.in_(accounts_by_email.keys())) \
                    .options(subqueryload(AttendeeAccount.attendees)).all()
                for account in existing_accounts:
                    existing_key = account.normalized_email
                    accounts_by_email.pop(existing_key, {})
                accounts = list(chain(*accounts_by_email.values()))

            if models and which_import == 'groups':
                groups = models
                groups_by_name = groupify(groups, lambda g: g['name'])

                """existing_groups = session.query(Group).filter(Group.name.in_(groups_by_name.keys())) \
                    .options(subqueryload(Group.attendees)).all()
                for group in existing_groups:
                    existing_key = group.name
                    groups_by_name.pop(existing_key, {})"""
                groups = list(chain(*groups_by_name.values()))

        return {
            'target_server': target_server,
            'api_token': api_token,
            'query': query,
            'message': message,
            'which_import': which_import,
            'unknown_ids': results.get('unknown_ids', []),
            'unknown_emails': results.get('unknown_emails', []),
            'unknown_names': results.get('unknown_names', []),
            'unknown_names_and_emails': results.get('unknown_names_and_emails', []),
            'attendees': attendees,
            'existing_attendees': existing_attendees,
            'accounts': accounts,
            'existing_accounts': existing_accounts,
            'groups': groups,
            'existing_groups': existing_groups,
        }

    def confirm_import_attendees(self, session, badge_type, admin_notes, target_server, api_token, query, attendee_ids):
        if cherrypy.request.method != 'POST':
            raise HTTPRedirect('import_attendees?target_server={}&api_token={}&query={}',
                               target_server,
                               api_token,
                               query)

        admin_id = cherrypy.session.get('account_id')
        admin_name = session.admin_attendee().full_name
        already_queued = 0
        attendee_ids = attendee_ids if isinstance(attendee_ids, list) else [attendee_ids]

        for id in attendee_ids:
            existing_import = session.query(ApiJob).filter(ApiJob.job_name == "attendee_import",
                                            ApiJob.query == id,
                                            ApiJob.cancelled == None,
                                            ApiJob.errors == '').count()
            if existing_import:
                already_queued += 1
            else:
                session.add(ApiJob(
                    admin_id = admin_id,
                    admin_name = admin_name,
                    job_name = "attendee_import",
                    target_server = target_server,
                    api_token = api_token,
                    query = id,
                    json_data = {'badge_type': badge_type, 'admin_notes': admin_notes, 'full': True}
                ))
            session.commit()

        attendee_count = len(attendee_ids) - already_queued
        badge_label = c.BADGES[int(badge_type)].lower()

        if len(attendee_ids) > 100:
            query = '' # Clear very large queries to prevent 502 errors

        raise HTTPRedirect(
            'import_attendees?target_server={}&api_token={}&query={}&message={}',
            target_server,
            api_token,
            query,
            '{count} attendee{s} imported with {a}{badge_label} badge{s}.{queued}'.format(
                count=attendee_count,
                s=pluralize(attendee_count),
                a=pluralize(attendee_count, singular='an ' if badge_label.startswith('a') else 'a ', plural=''),
                badge_label=badge_label,
                queued='' if not already_queued else ' {} badges are already queued for import.'.format(already_queued),
            )
        )

    def confirm_import_attendee_accounts(self, session, target_server, api_token, query, account_ids):
        if cherrypy.request.method != 'POST':
            raise HTTPRedirect('import_attendees?target_server={}&api_token={}&query={}&which_import={}',
                               target_server,
                               api_token,
                               query,
                               'accounts')

        admin_id = cherrypy.session.get('account_id')
        admin_name = session.admin_attendee().full_name
        already_queued = 0
        account_ids = account_ids if isinstance(account_ids, list) else [account_ids]

        for id in account_ids:
            existing_import = session.query(ApiJob).filter(ApiJob.job_name == "attendee_account_import",
                                            ApiJob.query == id,
                                            ApiJob.completed == None,
                                            ApiJob.cancelled == None,
                                            ApiJob.errors == '').count()
            if existing_import:
                already_queued += 1
            else:
                session.add(ApiJob(
                    admin_id = admin_id,
                    admin_name = admin_name,
                    job_name = "attendee_account_import",
                    target_server = target_server,
                    api_token = api_token,
                    query = id,
                    json_data = {'all': False}
                ))
            session.commit()

        attendee_count = len(account_ids) - already_queued

        if len(account_ids) > 100:
            query = '' # Clear very large queries to prevent 502 errors

        raise HTTPRedirect(
            'import_attendees?target_server={}&api_token={}&query={}&message={}&which_import={}',
            target_server,
            api_token,
            query,
            '{count} attendee account{s} queued for import.{queued}'.format(
                count=attendee_count,
                s=pluralize(attendee_count),
                queued='' if not already_queued else ' {} accounts are already queued for import.'.format(already_queued),
            ),
            'accounts',
        )
    
    def confirm_import_groups(self, session, target_server, api_token, query, group_ids):
        if cherrypy.request.method != 'POST':
            raise HTTPRedirect('import_attendees?target_server={}&api_token={}&query={}&which_import={}',
                               target_server,
                               api_token,
                               query,
                               'groups')

        admin_id = cherrypy.session.get('account_id')
        admin_name = session.admin_attendee().full_name
        already_queued = 0
        group_ids = group_ids if isinstance(group_ids, list) else [group_ids]

        for id in group_ids:
            existing_import = session.query(ApiJob).filter(ApiJob.job_name == "group_import",
                                            ApiJob.query == id,
                                            ApiJob.completed == None,
                                            ApiJob.cancelled == None,
                                            ApiJob.errors == '').count()
            if existing_import:
                already_queued += 1
            else:
                session.add(ApiJob(
                    admin_id = admin_id,
                    admin_name = admin_name,
                    job_name = "group_import",
                    target_server = target_server,
                    api_token = api_token,
                    query = id,
                    json_data = {'all': True}
                ))
            session.commit()

        attendee_count = len(group_ids) - already_queued

        if len(group_ids) > 100:
            query = '' # Clear very large queries to prevent 502 errors

        raise HTTPRedirect(
            'import_attendees?target_server={}&api_token={}&query={}&message={}&which_import={}',
            target_server,
            api_token,
            query,
            '{count} group{s} queued for import.{queued}'.format(
                count=attendee_count,
                s=pluralize(attendee_count),
                queued='' if not already_queued else ' {} groups are already queued for import.'.format(already_queued),
            ),
            'groups',
        )
