"""Entrypoint: python run.py  (or: python -m app.main)"""

from app.main import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
