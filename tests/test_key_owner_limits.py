"""Owners can cap a self-service key's spend, expiry, rate and models; the cap is enforced at reservation."""
from test_backend_v2 import BackendCase, ORIGIN


class KeyOwnerLimitTests(BackendCase):
    def save(self, **fields):
        data = {"credit_limit": "", "expires_on": "", "rate_limit_per_minute": "60", "models": ["gpt-6.1-sol"], **fields}
        return self.client.post(f"/developer/keys/{self.key_id}/limits", data=data, headers=ORIGIN)

    def test_tiny_credit_limit_rejects_generation_before_dispatch(self):
        self.assertEqual(self.save(credit_limit="0.000001").status_code, 303)
        self.assertEqual(self.rows("SELECT owner_credit_limit_units FROM api_keys WHERE id=?", (self.key_id,))[0]["owner_credit_limit_units"], 1)
        response = self.generate()
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["code"], "key_credit_limit_exceeded")
        self.assertEqual(self.calls, [])

    def test_owner_cannot_raise_rate_or_pick_unpriced_models(self):
        self.assertEqual(self.save(rate_limit_per_minute="600").status_code, 400)
        self.assertEqual(self.save(models=["not-a-model"]).status_code, 400)
        self.assertEqual(self.save(models=[]).status_code, 400)

    def test_other_users_key_is_not_found(self):
        other = self.new_user()
        self.execute("UPDATE api_keys SET user_id=? WHERE id=?", (other, self.key_id))
        self.assertEqual(self.save().status_code, 404)

    def test_migration_is_recorded(self):
        self.assertEqual(self.rows("SELECT version FROM schema_migrations")[0]["version"], 1)
