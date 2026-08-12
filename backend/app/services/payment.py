import stripe
from app.core.config import settings
from typing import Dict, Any

# The credential seat stripe 7.9.0 actually exposes. There is no
# ``Stripe`` class in that release - the package is configured by
# assigning the key on the module - so ``from stripe import Stripe``
# raised ImportError while this module was being imported. Every call
# below already goes through the module (``stripe.Charge.create``,
# ``stripe.Refund.create``, ``stripe.error.StripeError``), so binding the
# key here is all that was ever needed.
#
# This is the module ``app/api/transactions.py`` imports ``process_payment``
# from, so an ImportError here is not contained to payments: it propagated
# through that router and, before this repair, cost the application its
# entire transactions surface.
stripe.api_key = settings.STRIPE_API_KEY

def process_payment(token: str, amount: float, currency: str) -> Dict[str, Any]:
    try:
        # Validate payment amount and currency
        if amount <= 0:
            raise ValueError("Payment amount must be greater than zero")
        if currency not in ["usd", "eur", "gbp"]:  # Add more supported currencies as needed
            raise ValueError("Unsupported currency")

        # Create a Stripe charge using the provided token
        charge = stripe.Charge.create(
            amount=int(amount * 100),  # Stripe expects amount in cents
            currency=currency,
            source=token,
            description="Vehicle purchase payment"
        )

        # Handle successful payment and update transaction status
        if charge.status == "succeeded":
            return {
                "success": True,
                "charge_id": charge.id,
                "amount": amount,
                "currency": currency,
                "status": charge.status
            }
        else:
            return {
                "success": False,
                "error": "Payment was not successful",
                "status": charge.status
            }

    except stripe.error.StripeError as e:
        # Handle payment errors and exceptions
        return {
            "success": False,
            "error": str(e),
            "status": "failed"
        }
    except Exception as e:
        # Handle unexpected errors
        return {
            "success": False,
            "error": "An unexpected error occurred",
            "status": "failed"
        }

def create_refund(charge_id: str, amount: float) -> Dict[str, Any]:
    try:
        # Validate refund amount
        if amount <= 0:
            raise ValueError("Refund amount must be greater than zero")

        # Create a Stripe refund for the specified charge
        refund = stripe.Refund.create(
            charge=charge_id,
            amount=int(amount * 100)  # Stripe expects amount in cents
        )

        # Handle successful refund and update transaction status
        if refund.status == "succeeded":
            return {
                "success": True,
                "refund_id": refund.id,
                "amount": amount,
                "status": refund.status
            }
        else:
            return {
                "success": False,
                "error": "Refund was not successful",
                "status": refund.status
            }

    except stripe.error.StripeError as e:
        # Handle refund errors and exceptions
        return {
            "success": False,
            "error": str(e),
            "status": "failed"
        }
    except Exception as e:
        # Handle unexpected errors
        return {
            "success": False,
            "error": "An unexpected error occurred",
            "status": "failed"
        }