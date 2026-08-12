import stripe
from app.core.config import settings
from typing import Dict, Any

# stripe==7.9.0 (backend/requirements.txt) exposes a MODULE-LEVEL API and
# exports no `Stripe` class: `stripe.api_key` is the credential seat, and
# `stripe.Charge`, `stripe.Refund` and `stripe.error` are read off the module
# itself. The previous `from stripe import Stripe` / `Stripe(...)` form raised
# ImportError at import time, which blocked this module, the transactions
# router that imports process_payment, and therefore `app.main` as a whole.
# Every call site below is unchanged: `stripe` still resolves the same names.
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