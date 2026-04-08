import socket

from django.core.cache import cache
from django.db import connection


def check_database():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return {'status': 'ok', 'engine': connection.settings_dict.get('ENGINE')}


def check_cache():
    probe_key = 'judge_vortex:healthcheck'
    cache.set(probe_key, 'ok', timeout=5)
    value = cache.get(probe_key)
    cache.delete(probe_key)
    if value != 'ok':
        raise RuntimeError('Cache probe round-trip failed.')
    return {'status': 'ok'}


def check_tcp_dependency(host, port, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout):
        return {'status': 'ok', 'host': host, 'port': port}
