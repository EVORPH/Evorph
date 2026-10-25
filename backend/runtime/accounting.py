from datetime import datetime, timezone
import uuid

from evorph.contracts import InputError, canonical, digest, exact, identifier, integer
from .store import CapacityError, ConflictError


class Accounting:
    def __init__(self, store):
        self.store = store

    def configure(self, daily_micros, monthly_micros):
        integer(daily_micros, 1, 2**53 - 1)
        integer(monthly_micros, daily_micros, 2**53 - 1)
        with self.store.transaction():
            self.store.connection.execute('INSERT INTO resource_policy(policy_id,daily_micros,monthly_micros) VALUES(1,?,?) ON CONFLICT(policy_id) DO UPDATE SET daily_micros=excluded.daily_micros,monthly_micros=excluded.monthly_micros,paused=0', (daily_micros, monthly_micros))

    def pause(self):
        with self.store.transaction():
            self.store.connection.execute('UPDATE resource_policy SET paused=1 WHERE policy_id=1')

    @staticmethod
    def estimate(price, max_input, max_output):
        exact(price, {'input_micros_per_million', 'output_micros_per_million'})
        for value in price.values():
            integer(value, 0, 2**53 - 1)
        integer(max_input, 0, 10000000)
        integer(max_output, 1, 10000000)
        numerator = max_input * price['input_micros_per_million'] + max_output * price['output_micros_per_million']
        amount = (numerator + 999999) // 1000000
        integer(amount, 1, 2**53 - 1)
        return amount

    def get(self, call_id):
        row = self.store.connection.execute('SELECT * FROM model_calls WHERE call_id=?', (call_id,)).fetchone()
        if not row:
            raise InputError('Unknown model request.')
        return dict(row)

    def reserve(self, request_key, role, model_id, price, max_input, max_output, public_context_digest):
        identifier(request_key)
        identifier(public_context_digest)
        if role not in ('seeker', 'keeper') or type(model_id) is not str or not model_id or len(model_id) > 200 or any(ord(char) < 32 for char in model_id):
            raise InputError('Invalid model request metadata.')
        amount = self.estimate(price, max_input, max_output)
        request = {'role': role, 'model_id': model_id, 'price': price, 'max_input': max_input,
                   'max_output': max_output, 'context_digest': public_context_digest}
        raw = canonical(request)
        now = datetime.fromtimestamp(self.store.clock(), timezone.utc)
        day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
        db = self.store.connection
        with self.store.transaction():
            prior = db.execute('SELECT call_id,request_digest FROM model_calls WHERE request_key=?', (request_key,)).fetchone()
            if prior:
                if prior['request_digest'] != digest(raw):
                    raise ConflictError('Request key belongs to a different model call.')
                return prior['call_id']
            policy = db.execute('SELECT * FROM resource_policy WHERE policy_id=1').fetchone()
            if not policy or policy['paused']:
                raise CapacityError('Model execution is not enabled.')
            pending = db.execute("SELECT COALESCE(SUM(reserved_micros),0) FROM model_calls WHERE state IN ('reserved','dispatching','response_saved','usage_unknown')").fetchone()[0]
            daily = db.execute("SELECT COALESCE(SUM(actual_micros),0) FROM model_calls WHERE state='settled' AND day=?", (day,)).fetchone()[0]
            monthly = db.execute("SELECT COALESCE(SUM(actual_micros),0) FROM model_calls WHERE state='settled' AND month=?", (month,)).fetchone()[0]
            if daily + pending + amount > policy['daily_micros'] or monthly + pending + amount > policy['monthly_micros']:
                raise CapacityError('Research allocation is currently unavailable.')
            self.store.capacity(len(raw) * 4)
            call_id = 'call-' + uuid.uuid4().hex
            db.execute("INSERT INTO model_calls(call_id,request_key,request_digest,request_json,role,model_id,state,reserved_micros,day,month,created_at) VALUES(?,?,?,?,?,?,'reserved',?,?,?,?)", (call_id, request_key, digest(raw), raw, role, model_id, amount, day, month, self.store.clock()))
            return call_id

    def dispatch(self, call_id):
        with self.store.transaction():
            row = self.get(call_id)
            if row['state'] != 'reserved':
                raise ConflictError('Model request cannot be dispatched again.')
            policy = self.store.connection.execute('SELECT * FROM resource_policy WHERE policy_id=1').fetchone()
            if not policy or policy['paused']:
                raise CapacityError('Model execution is not enabled.')
            now = datetime.fromtimestamp(self.store.clock(), timezone.utc)
            day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
            pending = self.totals()['reserved_micros']
            daily = self.store.connection.execute("SELECT COALESCE(SUM(actual_micros),0) FROM model_calls WHERE state='settled' AND day=?", (day,)).fetchone()[0]
            monthly = self.store.connection.execute("SELECT COALESCE(SUM(actual_micros),0) FROM model_calls WHERE state='settled' AND month=?", (month,)).fetchone()[0]
            if daily + pending > policy['daily_micros'] or monthly + pending > policy['monthly_micros']:
                raise CapacityError('Research allocation is currently unavailable.')
            self.store.connection.execute("UPDATE model_calls SET state='dispatching',day=?,month=? WHERE call_id=?", (day, month, call_id))

    def cancel_before_send(self, call_id):
        with self.store.transaction():
            row = self.get(call_id)
            if row['state'] == 'cancelled_before_send':
                return
            if row['state'] != 'reserved':
                raise ConflictError('A dispatched request requires reconciliation.')
            self.store.connection.execute("UPDATE model_calls SET state='cancelled_before_send' WHERE call_id=?", (call_id,))

    def record_response(self, call_id, response):
        raw = canonical(response)
        if len(raw) > 1048576:
            raise InputError('Model response exceeds storage limit.')
        with self.store.transaction():
            row = self.get(call_id)
            if row['response_json'] is not None:
                if row['response_json'] != raw:
                    raise ConflictError('Model response is immutable.')
                return
            if row['state'] not in ('dispatching', 'usage_unknown'):
                raise ConflictError('Unexpected response state.')
            self.store.capacity(len(raw) * 4)
            self.store.connection.execute("UPDATE model_calls SET state='response_saved',response_json=? WHERE call_id=?", (raw, call_id))

    def mark_unknown(self, call_id):
        with self.store.transaction():
            row = self.get(call_id)
            if row['state'] == 'usage_unknown':
                return
            if row['state'] not in ('dispatching', 'response_saved'):
                raise ConflictError('Unexpected unconfirmed usage state.')
            self.store.connection.execute("UPDATE model_calls SET state='usage_unknown' WHERE call_id=?", (call_id,))

    def settle(self, call_id, actual_micros):
        integer(actual_micros, 0, 2**53 - 1)
        with self.store.transaction():
            row = self.get(call_id)
            if row['state'] == 'settled':
                if row['actual_micros'] != actual_micros:
                    raise ConflictError('Settled usage is immutable.')
                return
            if row['state'] != 'response_saved':
                raise ConflictError('Usage evidence must be saved before settlement.')
            self.store.connection.execute("UPDATE model_calls SET state='settled',actual_micros=? WHERE call_id=?", (actual_micros, call_id))
            if actual_micros > row['reserved_micros']:
                self.store.connection.execute('UPDATE resource_policy SET paused=1 WHERE policy_id=1')

    def totals(self):
        row = self.store.connection.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual_micros ELSE 0 END),0) AS settled_micros, COALESCE(SUM(CASE WHEN state IN ('reserved','dispatching','response_saved','usage_unknown') THEN reserved_micros ELSE 0 END),0) AS reserved_micros FROM model_calls").fetchone()
        return dict(row)
