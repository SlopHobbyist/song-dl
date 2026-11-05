"""Errors whose message is meant for the person using the site."""


class UserError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
