"""Точка входа для хостинга Railway.

Railway собирает проект из корня репозитория, а бот лежит в папке lab3.
Этот файл переходит в lab3 и запускает bot.py — так не нужно настраивать
Root Directory в интерфейсе Railway.
"""

import os
import runpy
import sys

BOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab3")

os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)
runpy.run_path(os.path.join(BOT_DIR, "bot.py"), run_name="__main__")
