"""Part 4, test 2: validation. Incomplete or malformed emails are rejected, a
clarification is sent to the AE, and nothing is created anywhere."""
import unittest
from tests.helpers import make_system, email, GOOD_BODY
from providers.llm import MockLLM


class IntakeValidation(unittest.TestCase):
    def _assert_rejected(self, body, expect_missing, llm=None, subject="Deal update"):
        sysm, m = make_system(llm=llm)
        out = sysm.handle(email(body, subject=subject))
        self.assertEqual(out.status, "rejected")
        for f in expect_missing:
            self.assertIn(f, out.reason)
        self.assertEqual(m["rocketlane"].create_calls, 0, "no project may be created")
        self.assertEqual(m["voice"].calls, [], "no call may be placed before validation passes")
        self.assertEqual(len(m["mail"].sent), 1, "exactly one clarification email")
        self.assertEqual(m["mail"].sent[0]["to"], "alex.rivera@novacrm.example")
        return m

    def test_missing_customer_name(self):
        # Subject deliberately carries no "Closed Won: <name>" pattern, so neither the body
        # nor the subject supplies a customer name.
        self._assert_rejected(GOOD_BODY.replace("Customer: Acme Foods\n", ""), ["customer_name"], subject="Deal update")

    def test_subject_supplies_customer_name(self):
        """'Closed Won: Acme Foods' in the subject is an acceptable source for the customer
        name when the body omits it. It is extracted, not guessed."""
        sysm, m = make_system()
        out = sysm.handle(email(GOOD_BODY.replace("Customer: Acme Foods\n", ""), subject="Closed Won: Acme Foods"))
        self.assertEqual(out.status, "completed")
        self.assertIn("acme foods", m["rocketlane"].projects, "project keyed by the subject-derived customer name")
        self.assertEqual(m["rocketlane"].create_calls, 1)

    def test_missing_contact_email(self):
        self._assert_rejected(GOOD_BODY.replace("Contact email: ops@acme.example\n", ""), ["customer_contact_email"])

    def test_missing_ae_and_link(self):
        self._assert_rejected("Customer: Acme Foods\nContact email: ops@acme.example\n", ["ae_name", "opportunity_link"])

    def test_malformed_email_address(self):
        self._assert_rejected(GOOD_BODY.replace("ops@acme.example", "ops-at-acme"), ["customer_contact_email"])

    def test_malformed_link(self):
        self._assert_rejected(GOOD_BODY.replace("https://novacrm.lightning.force.com/lightning/r/Opportunity/006ACME/view", "ask me"),
                              ["opportunity_link"])

    def test_empty_body(self):
        self._assert_rejected("", ["customer_name", "customer_contact_email", "ae_name", "opportunity_link"])

    def test_llm_empty_strings_are_still_missing(self):
        """The model returns empty strings for everything; validation treats them as absent."""
        llm = MockLLM({"customer_name": "", "customer_contact_email": "", "ae_name": "", "opportunity_link": ""})
        self._assert_rejected("We closed a deal, go.", ["customer_name"], llm=llm)

    def test_llm_cannot_invent_a_well_formed_value(self):
        """The dangerous case: the model returns a plausible, well-formed link and contact that the
        email never contained. Format validation would accept them. The appears-in-email check
        rejects them, and the audit log says so."""
        llm = MockLLM({"customer_name": "Acme Foods", "customer_contact_email": "billing@acme-foods.example",
                       "ae_name": "Alex Rivera", "opportunity_link": "https://novacrm.lightning.force.com/opp/006FAKE", "ae_email": None})
        m = self._assert_rejected("Just closed Acme Foods today, will send details. Alex",
                                  ["customer_contact_email", "opportunity_link"], llm=llm)
        import json, os
        with open(os.path.join(m["dir"], "audit.jsonl")) as f:
            actions = [json.loads(l)["action"] for l in f if l.strip()]
        self.assertIn("llm_values_rejected", actions)

    def test_llm_fills_free_text_email_when_fields_are_real(self):
        """Free-text email, no labels: the LLM extracts values that really are in the email,
        validation passes, and the tier is still confirmed by voice rather than taken from the text."""
        llm = MockLLM({"customer_name": "Acme Foods", "customer_contact_email": "ops@acme.example",
                       "ae_name": "Alex Rivera", "opportunity_link": "https://novacrm.lightning.force.com/lightning/r/Opportunity/006ACME/view", "ae_email": None})
        sysm, m = make_system(llm=llm)
        out = sysm.handle(email("Just closed Acme Foods, it's Enterprise, ops@acme.example is the contact, opp is "
                                "https://novacrm.lightning.force.com/lightning/r/Opportunity/006ACME/view. Alex Rivera"))
        self.assertEqual(out.status, "completed")
        self.assertEqual(len(m["voice"].calls), 1, "tier in the email text is ignored; the AE is still called")

    def test_signature_lines_do_not_overwrite_the_customer(self):
        """'Company:' in the AE's signature and 'Account:' in a CRM footer both match the customer
        label. The first match (what the AE typed) wins; later ones are ignored."""
        body = GOOD_BODY + "\n--\nAlex Rivera, AE\nCompany: NovaCRM Inc.\nAccount: 4471002\n"
        sysm, m = make_system()
        out = sysm.handle(email(body, subject="Closed Won: Acme Foods"))
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.deal.customer_name, "Acme Foods")

    def test_malformed_contact_reaches_the_validator(self):
        """A value that matches the label regex but fails the email validator (no dot in the
        domain) must come back as invalid, not silently pass."""
        self._assert_rejected(GOOD_BODY.replace("ops@acme.example", "ops@acmelocal"), ["customer_contact_email"])
