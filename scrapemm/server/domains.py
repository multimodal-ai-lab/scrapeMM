"""Which part of a host name is the registrable domain, the unit that challenges, the
blacklist and the browser slots are kept by (see `util.get_domain()`)."""

# The second levels under which countries register domains (gov.ua, co.uk, com.au, gob.ve)
PUBLIC_SECOND_LEVELS = {"ac", "co", "com", "edu", "gob", "go", "gouv", "gov", "govt", "gv", "ltd",
                        "mil", "ne", "net", "nhs", "nic", "nom", "or", "org", "plc", "sch"}


def is_public_suffix(domain: str) -> bool:
    """Whether `domain` is a country's public second level (gov.ua, co.uk), under which
    others register theirs, rather than a registrable domain itself."""
    labels = domain.lower().split('.')
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in PUBLIC_SECOND_LEVELS
