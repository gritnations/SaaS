"""The platform API package.

``api.main`` owns FastAPI composition and routing. It holds no SQL, no payment-provider code and no
business logic. Global ingress controls live one level up, in the root ``main.py``.
"""
