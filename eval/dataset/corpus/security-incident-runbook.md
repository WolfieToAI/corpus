# Security incident runbook

This runbook tells engineers what to do when a security incident is suspected.

## Severity levels

- SEV1: active exploitation or confirmed leak of customer data. The on-call engineer must
  acknowledge a SEV1 page within 15 minutes, and the incident commander informs the CTO
  immediately.
- SEV2: a vulnerability that is exploitable but not known to be exploited. Acknowledge within
  1 hour; a fix or mitigation must ship within 72 hours.
- SEV3: low-risk issue, tracked in the backlog and fixed within 30 days.

## First steps

1. Open an incident channel named after the date, for example inc-2026-11-15.
2. Name an incident commander; the commander coordinates and does not debug.
3. Preserve evidence: snapshot the affected hosts before any change and never delete logs.
4. Rotate any credential that may have been exposed, starting with production database
   passwords and cloud access keys.

## Customer communication

If customer data may be affected, the legal team must be involved within 24 hours. Under the
GDPR, the supervisory authority is notified within 72 hours after the company becomes aware of
a personal data breach. Customer emails are sent only after approval by legal.

## After the incident

A blameless postmortem is written within 5 business days for every SEV1 and SEV2. It lists the
timeline, the root cause and action items with owners and due dates.
