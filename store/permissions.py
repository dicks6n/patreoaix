from rest_framework.permissions import BasePermission
from django.core.exceptions import ObjectDoesNotExist

class IsKYCVerified(BasePermission):
    message = "Account verification (KYC) is required to execute trades."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        try:
            return request.user.userprofile.is_kyc_verified
        except ObjectDoesNotExist:
            return False