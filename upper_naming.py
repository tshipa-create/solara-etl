from dlt.common.normalizers.naming.snake_case import NamingConvention as SnakeCase

class UpperSnakeCase(SnakeCase):
    def normalize_identifier(self, identifier: str) -> str:
        # 1. Clean the name using standard rules (removes spaces, special chars)
        cleaned = super().normalize_identifier(identifier)
        # 2. Force it to UPPERCASE
        return cleaned.upper()