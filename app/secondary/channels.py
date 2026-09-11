"""Explicit CRM source routing takes precedence over contact fallbacks."""

def source_channel(source):
    value = str(source or '').strip().casefold()
    if 'agent' in value:
        return 'linkedin'
    if value in {'crmgen_jin', 'crm跟进'}:
        return 'email'
    return None


def outbound_channel(record, latest=None):
    forced = source_channel((record.get('lead') or {}).get('source') or record.get('lead_source'))
    return forced or (latest or {}).get('channel') or (record.get('output') or {}).get('type') or 'email'
