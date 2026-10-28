# WhatsApp for Flow Steward

`flowsteward.whatsapp` 0.1.0 connects a Flow Steward project to the **WhatsApp Business Cloud API**
(Meta). It sends messages, templates, reactions and files from workflows, delivers project
notifications, and starts workflows from incoming messages and delivery statuses.

## What it does

Workflow steps (all through one transport module, all suppressed in a Test Run):

| Step | What it does |
| --- | --- |
| Send WhatsApp message | Text up to 4096 characters, optional link preview, optional reply to a message, optional 1-3 reply buttons (interactive message; text then at most 1024 characters). A longer text is refused with a clear error, never cut. |
| Send WhatsApp template | An approved template with positional body text parameters. The only message type allowed outside the 24-hour window. |
| Send WhatsApp reaction | An emoji reaction to a message a contact sent you. |
| Mark WhatsApp message as read | Blue ticks for a received message. |
| Send WhatsApp file | Image (5 MB), document (100 MB), audio (16 MB) or video (16 MB); size and MIME type are checked before anything is uploaded. |
| Get WhatsApp file | Downloads a received media (by media ID) into an artifact. |

Triggers: **New WhatsApp message** (`whatsapp.message.received`) and **WhatsApp message status**
(`whatsapp.message.status`: sent, delivered, read, failed).

Notifications: `deliver_notification` sends plain text to contacts enabled for **Notify**.

A message starts a workflow only from a **contact you saved and switched Workflow trigger on for**
(closed by default). Everyone who writes to the number is listed under *Discovered senders*.

## Setting it up (Meta dashboard)

1. Create a Meta app with the WhatsApp product; note the **Phone number ID** (WhatsApp > API setup).
2. In Meta Business Settings create a **system user**, assign the app with the
   `whatsapp_business_messaging` permission and generate an **access token**.
3. Copy the app's **App secret** (App settings > Basic).
4. In Flow Steward open the extension, **Setup**: save the access token and app secret, save the phone
   number ID, press **Verify**.
5. Incoming messages: copy the **callback URL** and the **verify token** from the webhook card into
   Meta (WhatsApp > Configuration > Webhook) and subscribe to the **messages** field. Flow Steward answers
   Meta's handshake and checks `X-Hub-Signature-256` with the app secret before the extension sees a payload.
6. **Contacts**: add the people this number may write to (phone number, digits only with country code).

## What it cannot do

- **No edit, delete or pin** of sent messages: WhatsApp has none.
- **24-hour window.** Free-form messages are refused (error 131047) unless the person wrote to you within
  24 hours; use *Send WhatsApp template*, or wait for them to write first. Only people who opted in may be messaged.
- **Incoming messages need a public HTTPS URL Meta can reach.** WhatsApp has no polling; a local install
  cannot receive messages (sending still works).
- **Groups are not supported** in 0.1.0.
- **Approvals through buttons are not supported** in 0.1.0 (no human-decision capability). A button tap
  arrives as an ordinary *New WhatsApp message* event with `button_id` and `button_title`.
- Template header and button parameters are not supported (body text parameters only).

## Requirements

Speaks the host contract `extension_host_v2` and is built on `flowsteward-extension-sdk` 0.4.0.

TODO(release): state the minimum Core version. The extension needs a Core that (1) supports the
`hub_signature` callback authentication (Meta's GET handshake and `X-Hub-Signature-256` check; in progress
on Core branch `codex/webhook-hub-signature`) and (2) runs provider-event triggers. No `platform_min` is
declared until that Core is versioned.

## Bundle layout

- `extension.yaml`: manifest, triggers, external effects, callback wiring
- `main.py`: subprocess entrypoint (queries, actions, delivery, ingress)
- `main_transport.py`: the only network code: Graph calls, error mapping, redaction, media URL check
- `main_send.py`: Send message / template / reaction / Mark as read, contact selection, button rules
- `main_steps.py`: Send file and Get file
- `main_delivery.py`: notification delivery
- `main_ingress.py`: webhook payloads to events, dedupe, closed-by-default gating
- `main_state.py`, `main_queries.py`, `main_actions.py`, `main_readiness.py`: state, UI queries/actions, readiness
- `contracts/`, `schemas/`, `ui/`, `locales/` (en, lt): declarations
- `docs/whatsapp-cloud-api-facts.md`: the verified API facts; `docs/DECISIONS.md`: decisions and open questions
- `tests/`: tests with the transport mocked (no network, no real token)

## Configuration

Connection type `whatsapp_cloud`: `phone_number_id` (required for any call), `waba_id` (optional),
`graph_api_version` (default `v26.0`), `contacts[]` (`channel_id`, `wa_id`, `display_name`, `enabled`,
`capabilities {notify, workflow_trigger}`). Secrets: `access_token` (required), `app_secret` (needed for
incoming messages). Secrets are stored by the platform, never in config, and never appear in a log, error or result.

## Trigger event payload

`message_received`: `text` (or caption), `message_type`, `wa_id`, `profile_name`, `message_id` (wamid),
`reply_to_message_id`, `has_file`, `file_id` (media ID), `file_kind`, `file_name`, `mime_type`, `button_id`,
`button_title`, location fields, `timestamp`, `phone_number_id`, `dedupe_key`, `project_id`, `connection_id`.
`message_status`: `status`, `message_id`, `wa_id`, `timestamp`, `error_code` / `error_title` for a failure.
Media IDs received in webhooks are kept for 7 days; download links live 5 minutes.
