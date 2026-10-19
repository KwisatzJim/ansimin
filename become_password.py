#!/usr/bin/python3
"""Ansible password helper: read only the parent's anonymous memory file."""
import os
import sys


def main():
    location = os.environ.get('ANSIMIN_PASSWORD_FD', '')
    pieces = location.split(':')
    if len(pieces) != 2 or not all(piece.isdecimal() for piece in pieces):
        raise SystemExit('Missing ansimin credential channel')
    pid, fd = pieces
    with open(f'/proc/{pid}/fd/{fd}', 'rb') as source:
        secret = source.read(514)
    if not secret or len(secret) > 513:
        raise SystemExit('Invalid ansimin credential channel')
    sys.stdout.buffer.write(secret)


if __name__ == '__main__':
    main()
