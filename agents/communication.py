"""Agent 2: Communication.

Creates the shared Slack channel and posts a welcome that is personalised to the
tier and the customer. Runs only after the Intake Agent has a confirmed tier and a
created project, so it never has to guess either.

Sharing: the AE is invited by email. The customer contact is invited by email too;
if they are not in the workspace (they will not be, they are a customer) the audit
log records that Slack Connect is the production path. Nothing is hidden.
"""
from __future__ import annotations
from models import DealNotification, PlanTier, RocketlaneProject, SlackChannelResult
from providers.slack import slack_channel_name, slack_escape, SlackError
from templates import template_for


def welcome_message(deal: DealNotification, tier: PlanTier, project: RocketlaneProject) -> str:
    cust, ae, pname = slack_escape(deal.customer_name), slack_escape(deal.ae_name), slack_escape(project.project_name)
    if tier == PlanTier.ENTERPRISE:
        csm_line = "You have a dedicated Customer Success Manager for the whole 30-day onboarding, and this channel is the fastest way to reach them."
    else:
        csm_line = "Your onboarding runs 14 days with our pooled CSM team, so anyone here can pick up your question during business hours."
    link = f" ({project.url})" if project.url else ""
    return (f"Welcome to NovaCRM, {cust} team!\n\n"
            f"This is your shared onboarding channel. {csm_line}\n\n"
            f"What happens next: Kickoff, Data Migration, Configuration, Go-Live. "
            f"Your plan is tracked in Rocketlane as '{pname}'{link}.\n\n"
            f"One thing we will ask of you early: after we migrate your data, we will ask you to verify it before we go live. "
            f"That step is yours and we will not skip it.\n\n"
            f"Your account executive {ae} stays in the loop here too.")


class CommunicationAgent:
    name = "communication_agent"

    def __init__(self, *, slack, audit):
        self.slack, self.audit = slack, audit

    def run(self, deal: DealNotification, tier: PlanTier, project: RocketlaneProject) -> SlackChannelResult:
        cid_corr = deal.source_message_id
        tpl = template_for(tier)
        name = slack_channel_name(deal.customer_name, tier.value)
        topic = slack_escape(f"{deal.customer_name} onboarding | {tier.value.title()} plan, {tpl.onboarding_days} days, "
                             f"{tpl.csm_model} CSM | Rocketlane #{project.project_id} | customer contact {deal.customer_contact_email}")
        try:
            channel_id = self.slack.create_channel(name)
        except SlackError as e:
            if "name_taken" not in str(e):
                raise
            name = f"{name}-{project.project_id}"
            channel_id = self.slack.create_channel(name)
            self.audit.record(self.name, "channel_name_collision", outputs={"new_name": name},
                              rationale="A channel with the customer's name already existed; suffixed with the project id rather than reusing an unknown channel.",
                              level="warning", correlation_id=cid_corr)
        # Audit the moment something real exists, before the topic, welcome or invites can fail,
        # so any escalation after this point carries the channel id.
        self.audit.record(self.name, "channel_created", inputs={"customer": deal.customer_name, "tier": tier.value},
                          outputs={"channel_id": channel_id, "name": name},
                          rationale="Channel exists. Topic, welcome and invites follow and are audited separately.",
                          correlation_id=cid_corr)
        self.slack.set_topic(channel_id, topic)
        msg = welcome_message(deal, tier, project)
        self.slack.post_message(channel_id, msg)
        invitees = [e for e in (deal.ae_email, deal.customer_contact_email) if e]
        invited = self.slack.invite_by_email(channel_id, invitees)
        self.audit.record(self.name, "channel_invites", inputs={"emails": invitees}, outputs=invited,
                          rationale="AE invited if in the workspace. A customer contact outside the workspace is expected; production uses Slack Connect for them.",
                          correlation_id=cid_corr)
        result = SlackChannelResult(channel_id=channel_id, channel_name=name, topic=topic, welcome_message=msg)
        self.audit.record(self.name, "channel_ready", inputs={"customer": deal.customer_name, "tier": tier.value},
                          outputs={"channel_id": channel_id, "name": name, "topic": topic},
                          rationale="Channel, topic and welcome personalised from the confirmed tier and customer details.",
                          correlation_id=cid_corr)
        return result
