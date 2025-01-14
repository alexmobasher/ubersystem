from sqlalchemy import or_, and_

from uber.config import c
from uber.decorators import all_renderable, log_pageview
from uber.models import Attendee, Group, PromoCode, ReceiptTransaction, ModelReceipt


@all_renderable()
class Root:
    def comped_badges(self, session, message='', show='all'):
        regular_comped = session.attendees_with_badges().filter(Attendee.paid == c.NEED_NOT_PAY, 
                                                                Attendee.promo_code == None)
        promo_comped = session.query(Attendee).join(PromoCode).filter(Attendee.has_badge == True,
                                                                      Attendee.paid == c.NEED_NOT_PAY,
                                                                      or_(PromoCode.cost == None, 
                                                                          PromoCode.cost == 0))
        group_comped = session.query(Attendee).join(Group, Attendee.group_id == Group.id)\
                .filter(Attendee.has_badge == True, Attendee.paid == c.PAID_BY_GROUP, Group.cost == 0)
        all_comped = regular_comped.union(promo_comped, group_comped)
        claimed_comped = all_comped.filter(Attendee.placeholder == False)
        unclaimed_comped = all_comped.filter(Attendee.placeholder == True)
            
        return {
            'message': message,
            'comped_attendees': all_comped,
            'all_comped': all_comped.count(),
            'claimed_comped': claimed_comped.count(),
            'unclaimed_comped': unclaimed_comped.count(),
            'show': show,
        }

    def found_how(self, session):
        return {'all': sorted(
            [a.found_how for a in session.query(Attendee).filter(Attendee.found_how != '').all()],
            key=lambda s: s.lower())}
    
    @log_pageview
    def attendee_receipt_discrepancies(self, session, include_pending=False, page=1):
        filters = [Attendee.default_cost_cents != ModelReceipt.item_total]
        if include_pending:
            filters.append(or_(Attendee.badge_status.in_([c.PENDING_STATUS, c.AT_DOOR_PENDING_STATUS]), Attendee.is_valid == True))
        else:
            filters.append(Attendee.is_valid == True)

        receipt_query = session.query(Attendee).join(Attendee.active_receipt).filter(*filters)

        page = int(page)
        if page <= 0:
            offset = 0
        else:
            offset = (page - 1) * 50

        return {
            'current_page': page,
            'pages': (receipt_query.count() // 100) + 1,
            'attendees': receipt_query.limit(50).offset(offset),
            'include_pending': include_pending,
        }
    
    @log_pageview
    def attendees_nonzero_balance(self, session, include_no_receipts=False):
        attendees = session.query(Attendee, ModelReceipt).join(Attendee.active_receipt).filter(Attendee.default_cost_cents == ModelReceipt.item_total,
                                                                                                   ModelReceipt.current_receipt_amount != 0)

        return {
            'attendees': attendees.filter(Attendee.is_valid == True)
        }

    @log_pageview
    def self_service_refunds(self, session):
        refunds = session.query(ReceiptTransaction).filter_by(type=c.REFUND)

        refund_attendees = {}
        for refund in refunds:
            refund_attendees[refund.id] = refund.attendees[0].attendee if refund.attendees else None

        return {
            'refunds': refunds,
            'refund_attendees': refund_attendees,
        }
