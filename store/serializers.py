from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        try:
            token['is_kyc_verified'] = user.userprofile.is_kyc_verified
            token['subscription_tier'] = user.userprofile.subscription_tier
        except Exception:
            token['is_kyc_verified'] = False
            token['subscription_tier'] = 'free'
        return token