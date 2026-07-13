# -*- coding: utf-8 -*-
"""Печатает локальный IP-адрес компьютера в сети (для доступа с телефона)."""
import socket

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))          # реальное соединение не устанавливается
    print(s.getsockname()[0])
except OSError:
    print("127.0.0.1")
finally:
    s.close()
