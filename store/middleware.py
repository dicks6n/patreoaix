# middleware.py
import re
from django.utils.deprecation import MiddlewareMixin
from django.contrib.auth.models import AnonymousUser
from .models import UserSession


def parse_user_agent(ua_string):
    """Parse user agent to extract browser, OS, and device type"""
    if not ua_string:
        return {'browser': 'Unknown', 'os_name': 'Unknown', 'device_type': 'desktop'}
    
    ua = ua_string.lower()
    
    # Browser detection
    browser = 'Unknown'
    if 'edg/' in ua or 'edge/' in ua:
        browser = 'Edge'
    elif 'chrome/' in ua and 'safari/' in ua:
        browser = 'Chrome'
    elif 'firefox/' in ua:
        browser = 'Firefox'
    elif 'safari/' in ua and 'chrome/' not in ua:
        browser = 'Safari'
    elif 'opera' in ua or 'opr/' in ua:
        browser = 'Opera'
    elif 'msie' in ua or 'trident' in ua:
        browser = 'Internet Explorer'
    
    # OS detection
    os_name = 'Unknown'
    if 'windows' in ua:
        os_name = 'Windows'
    elif 'mac os' in ua or 'macintosh' in ua:
        os_name = 'macOS'
    elif 'linux' in ua and 'android' not in ua:
        os_name = 'Linux'
    elif 'android' in ua:
        os_name = 'Android'
    elif 'iphone' in ua or 'ipad' in ua or 'ipod' in ua:
        os_name = 'iOS'
    
    # Device type
    device_type = 'desktop'
    if 'mobile' in ua or 'iphone' in ua or ('android' in ua and 'mobile' in ua):
        device_type = 'mobile'
    elif 'tablet' in ua or 'ipad' in ua:
        device_type = 'tablet'
    
    return {'browser': browser, 'os_name': os_name, 'device_type': device_type}


def get_client_ip(request):
    """Get real client IP even behind proxies"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


class SessionTrackingMiddleware(MiddlewareMixin):
    """Tracks user sessions in the database on each request"""
    
    def process_request(self, request):
        # Skip for static/admin/anonymous
        if not hasattr(request, 'user') or isinstance(request.user, AnonymousUser):
            return None
        
        if not request.user.is_authenticated:
            return None
        
        # Skip paths we don't care about
        skip_paths = ['/static/', '/media/', '/admin/jsi18n/']
        if any(request.path.startswith(p) for p in skip_paths):
            return None
        
        try:
            session_key = request.session.session_key
            if not session_key:
                return None
            
            ua_info = parse_user_agent(request.META.get('HTTP_USER_AGENT', ''))
            ip = get_client_ip(request)
            
            # Try to find or create the session
            session, created = UserSession.objects.update_or_create(
                session_key=session_key,
                defaults={
                    'user': request.user,
                    'ip_address': ip,
                    'user_agent': request.META.get('HTTP_USER_AGENT', ''),
                    'browser': ua_info['browser'],
                    'os_name': ua_info['os_name'],
                    'device_type': ua_info['device_type'],
                }
            )
            
            # Mark all other sessions as not current, this one as current
            UserSession.objects.filter(user=request.user).exclude(id=session.id).update(is_current=False)
            if not session.is_current:
                session.is_current = True
                session.save(update_fields=['is_current'])
                
        except Exception as e:
            # Never break the request
            pass
        
        return None