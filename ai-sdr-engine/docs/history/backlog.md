# Backlog

## P1
- **[P1]** LinkedIn sends are recorded as sent when PhantomBuster accepts the launch; reconcile the message status from the run log (own-profile, pending invite, not-connected cases) — discovered during the LinkedIn Message Sender build

## P2
- **[P2]** A LinkedIn note queued as the Outreach follow-up is sent by PhantomBuster on acceptance, but the workspace only learns of it if someone re-checks; poll the workflow results and log the Message when it goes out — discovered during the LinkedIn send-confirmation fix
- **[P2]** Text messages can't be delivered on the current Twilio trial (WhatsApp needs an approved template, SMS needs a purchased number); the workspace keeps approved texts queued until the channel is upgraded — discovered during the integration test pass
