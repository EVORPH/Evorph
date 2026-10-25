"""Compose existing modules into runnable objects; no contract is redefined here."""

import time
from dataclasses import dataclass
from pathlib import Path

from agents.channel import ModelChannel
from agents.fixtures import ScriptedTransport, synthetic_meter
from agents.metering import context_bound_meter
from agents.openrouter import CurlHTTP, OpenRouterChannel, role_schemas
from agents.research import Research
from agents.scheduler import Scheduler
from evorph.contracts import InputError
from evorph.evidence import read_file
from runtime.accounting import Accounting
from runtime.store import Store
from service.public_store import PublicService, RELEASABLE, ReviewError, review_event

from .config import CREDENTIAL_BYTES

AUTO_REVIEWER = 'schedule-auto.v1'
# An inert channel still has to satisfy the credential encoding; it can never send
# because the transport stays disabled and request() fails closed before reserving.
INERT_CREDENTIAL = 'inert'


@dataclass
class Runtime:
    store: Store
    accounting: Accounting
    research: Research
    service: PublicService
    scheduler: Scheduler

    def close(self):
        self.store.close()


def credential(config, execute):
    """Only the process that may spend reads the credential; readers and the console do not."""
    if not (execute and config.transport_enabled):
        return INERT_CREDENTIAL
    if config.credential_path is None:
        raise InputError('Live channels require a credential path.')
    path = Path(config.credential_path)
    raw = read_file(path.parent, path.name, CREDENTIAL_BYTES)
    try:
        return raw.strip().decode('ascii')
    except UnicodeDecodeError as exc:
        raise InputError('Provider credential is not plain text.') from exc


def build_channel(accounting, spec, transport, config, execute):
    if transport['kind'] == 'offline':
        return ModelChannel(accounting, spec, ScriptedTransport(transport['script']), synthetic_meter)
    enabled = bool(execute and config.transport_enabled)
    http = CurlHTTP(credential(config, execute), enabled=enabled)
    return OpenRouterChannel(accounting, spec, http, provider_name=spec.provider_name,
                             schemas=role_schemas(),
                             meter=context_bound_meter(spec.max_input_tokens, spec.meter_basis))


def publisher(service, research):
    """Release a finished round through the existing review gate; unpublished records stay private."""
    def release(batch_id):
        reviewed = {}
        withheld = set()
        withheld_count = 0
        for event in research.journal(batch_id):
            if event['kind'] not in RELEASABLE:
                continue
            try:
                reviewed[event['record_id']] = review_event(event)
            except ReviewError as exc:
                withheld.add(exc.category)
                withheld_count += 1
        if reviewed:
            service.approve(batch_id, AUTO_REVIEWER, reviewed)
        service.publish(batch_id)
        service.refresh_experiment(batch_id)
        return {'state': 'partial' if withheld else 'published',
                'withheld_count': withheld_count, 'reason_categories': sorted(withheld)}
    return release


def build(config, *, clock=time.time, execute=False):
    """Stored spending policy is left untouched; a restart never unpauses the private ledger."""
    store = Store(config.state, config.limits, clock=clock)
    try:
        accounting = Accounting(store)
        channels = {}
        for role in ('seeker', 'keeper'):
            spec, transport = config.channels[role]
            channels[role] = build_channel(accounting, spec, transport, config, execute)
        research = Research(store, channels['seeker'], channels['keeper'], config.openssl, mode=config.mode)
        service = PublicService(store, research, config.service_limits, now=clock, mode=config.mode)
        scheduler = Scheduler(research, publisher=publisher(service, research))
    except BaseException:
        store.close()
        raise
    return Runtime(store, accounting, research, service, scheduler)
