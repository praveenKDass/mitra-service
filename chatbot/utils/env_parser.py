def load_env_to_dict(value):
    if value is None:
        return {}
    env_dict = {}
    for line in value.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env_dict[key.strip()] = val.strip().strip('"').strip("'")
    return env_dict