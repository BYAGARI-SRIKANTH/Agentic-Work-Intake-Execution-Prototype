class IntegrationError(Exception):
    def __init__(self, category, message, outcome_unknown=False):
        super().__init__(message)
        self.category = category
        self.message = message
        self.outcome_unknown = outcome_unknown
