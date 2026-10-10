from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


class CustomAPIException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_detail = "Validation error"

    def __init__(self, message=None, code=None, status_code=None, details=None):
        if message is not None:
            self.detail = message
        else:
            self.detail = self.default_detail

        self.error_code = code or self.default_code
        if status_code is not None:
            self.status_code = status_code
        self.details = details if details is not None else []
        super().__init__(self.detail, self.error_code)


class ValidationError(CustomAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "VALIDATION_ERROR"
    default_detail = "Bad or missing input"


class NotFound(CustomAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "NOT_FOUND"
    default_detail = "Resource not found"


class DuplicateError(CustomAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "DUPLICATE"
    default_detail = "Resource already exists"


class InsufficientStockError(CustomAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "INSUFFICIENT_STOCK"
    default_detail = "Insufficient stock"


class InvalidTransitionError(CustomAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "INVALID_TRANSITION"
    default_detail = "Invalid status transition"


class OrderLockedError(CustomAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "ORDER_LOCKED"
    default_detail = "Order is locked and cannot be edited"


class IdempotencyConflictError(CustomAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "IDEMPOTENCY_CONFLICT"
    default_detail = "Idempotency key reused with different request body"


class SupplierUnavailableError(CustomAPIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "SUPPLIER_UNAVAILABLE"
    default_detail = "Supplier feed unavailable"


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if isinstance(exc, CustomAPIException):
        response.data = {
            "error": {
                "code": exc.error_code,
                "message": str(exc.detail),
                "details": exc.details,
            }
        }
        return response

    if response is not None:
        code = "VALIDATION_ERROR"
        status_code = response.status_code
        if status_code == 404:
            code = "NOT_FOUND"
        elif status_code == 409:
            code = "DUPLICATE"
        elif status_code >= 500:
            code = "INTERNAL_SERVER_ERROR"

        details = []
        if isinstance(response.data, dict):
            if "detail" in response.data:
                msg = str(response.data["detail"])
            else:
                msg = "Validation failed"
                for field, err in response.data.items():
                    if isinstance(err, list):
                        for item in err:
                            details.append({"field": field, "message": str(item)})
                    else:
                        details.append({"field": field, "message": str(err)})
        elif isinstance(response.data, list):
            msg = "Validation failed"
            details = [{"message": str(item)} for item in response.data]
        else:
            msg = str(response.data)

        response.data = {
            "error": {
                "code": code,
                "message": msg,
                "details": details,
            }
        }

    return response
