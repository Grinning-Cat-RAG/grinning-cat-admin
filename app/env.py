import os


def get_supported_env_variables():
    return {
        "GRINNING_CAT_API_HOST": "localhost",
        "GRINNING_CAT_API_PORT": "1865",
        "GRINNING_CAT_API_KEY": None,
        "GRINNING_CAT_API_SECURE_CONNECTION": "false",
        "GRINNING_CAT_INTRO_MESSAGE": None,
        "GRINNING_CAT_CHECK_INTERVAL": "20",
        "GRINNING_CAT_JWT_EXPIRE_MINUTES": str(60 * 24),  # JWT expires after 1 day
        "GRINNING_CAT_ENVIRONMENT": "prod",
    }


def get_env(name):
    """Utility to get an environment variable value. To be used only for supported Cat envs.
    - covers default supported variables and their default value
    - automagically handles legacy env variables missing the prefix "CCAT_"
    """
    cat_default_env_variables = get_supported_env_variables()

    default = None
    if name in cat_default_env_variables:
        default = cat_default_env_variables[name]

    return os.getenv(name, default)


def get_env_bool(name):
    """Read a supported env variable as a boolean.

    The comparison is case-insensitive: a `True` written in the .env file must
    not silently turn into False, which for
    GRINNING_CAT_API_SECURE_CONNECTION would downgrade the connection to plain
    HTTP and send the credentials in clear.
    """
    value = get_env(name)
    return str(value).strip().lower() in ("1", "true") if value is not None else False
