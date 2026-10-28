# Decisions and open questions (WhatsApp extension 0.1.0)

Written while building from the Telegram 1.3.0 snapshot, without the owner available. Facts about
Meta's API are in `whatsapp-cloud-api-facts.md`; nothing here invents API behaviour.

## Decisions

- **D-1 Identity.** `flowsteward.whatsapp`, "WhatsApp", 0.1.0, `notification_provider`, connection type
  `whatsapp_cloud`, package `flowsteward-whatsapp-extension`, repo `ext-messaging-whatsapp`.
- **D-2 Contacts keep the key `channel_id`.** The contact list is `contacts[]` with `channel_id` (the
  contact's ID), `wa_id`, `display_name`, `enabled`, `capabilities {notify, workflow_trigger}`. The ID
  key stays `channel_id` so notification routes (`route_config.channel_id`), the platform list operations
  (`item_key: channel_id`) and external-effect observability (`target_fields: channel_id`) work exactly as
  they do for Telegram. Steps take `channel_id` (label "Contact ID") or, as an alternative, `wa_id` of a
  SAVED contact. A step never sends to a number that is not a saved, enabled, notify-capable contact
  (opt-in rule; also keeps the bot from becoming an open relay).
- **D-3 Closed by default.** `workflow_trigger` defaults to false everywhere (schema, UI default, parser,
  "save discovered sender"). A message becomes `whatsapp.message.received` only from an enabled contact
  with `capabilities.workflow_trigger`. Every sender is still remembered under "discovered senders".
- **D-4 `wa_id` is digits only**, 7 to 15 digits (E.164 maximum is 15; 7 is my floor, not Meta's). The
  connection schema enforces `^[0-9]{7,15}$`; the reader tolerates `+`, spaces, dashes and brackets when
  matching a sender. Known caveat (not handled): some countries' `wa_id` differs from the dialled number
  (for example an extra digit); then the owner saves the number exactly as WhatsApp reports it, which the
  discovered-senders list provides.
- **D-5 One transport function.** All network I/O is `main_transport._http_exchange`; tests replace only
  that. `graph_call`, upload, media info, download and the phone-number read sit on top of it.
- **D-6 Error mapping** (`main_transport.GRAPH_ERRORS`): every code was checked against Meta's error-code
  page (facts file). Retryable (and definitely not sent): 4, 80007, 130429, 131056. 131047 gets the
  "use Send template or wait for the person to write first" message. An unlisted code with a 4xx answer
  is `whatsapp_request_rejected` (definite); a 5xx or a lost connection is `whatsapp_send_ambiguous`
  (outcome unknown, never retried blindly). Graph's own message is shown (redacted) for unlisted codes.
  131016 "service unavailable" is mapped but NOT flagged retryable because Meta does not say whether the
  message was queued.
- **D-7 Templates: body text parameters only.** Meta's template overview documents the body shape; the
  header and button parameter shapes were not on any page I read, so they are not offered. Positional only
  (named parameters need `parameter_name`; not offered).
- **D-8 Media.** `send_file` kinds: image, document, audio, video (no sticker, no voice flag). Size limit
  and MIME type are checked BEFORE the upload: image 5 MB, document 100 MB, audio 16 MB, video 16 MB. The
  MIME type is the step input, else the artifact's content type, else guessed from the file name.
  `audio/ogg` is accepted but WhatsApp requires OPUS mono; the extension cannot inspect it, so WhatsApp's
  131053 answer is the safeguard. Audio takes no caption (refused with `caption_not_supported`).
  `get_file` refuses a declared or streamed size over 100 MB (the largest kind). The whole file is held in
  memory for the upload and the download (bounded by those limits).
- **D-9 Media download host.** Meta does not name the host of the temporary download URL. The bearer token
  is only sent to `graph.facebook.com` or `*.fbsbx.com`, `*.facebook.com`, `*.fbcdn.net` over https, with
  no embedded credentials; any other host is refused (`whatsapp_media_url_rejected`) without a request.
  `extension.yaml` egress lists `graph.facebook.com` and `lookaside.fbsbx.com` (the latter from experience,
  NOT from the docs). **Open question Q-1.**
- **D-10 Inbound.** Core authenticates (GET handshake with the verify token Core generates; POST signature
  `X-Hub-Signature-256` with the app secret). The extension's `ingress_event` refuses anything not marked
  `callback.authenticated`. Dedupe keys: wamid for a message, `wamid:status` for a status; the last 500 keys
  live in `ingress_config.seen_message_keys` (written with a `project_connection_upsert` host mutation). A
  Meta retry within that window emits nothing. Events for a phone number other than the configured one are
  ignored (one Meta app can subscribe several numbers).
- **D-11 Statuses are always emitted** as `whatsapp.message.status`; Core filters by the trigger's `status`
  / `wa_id` config. (Every outbound message produces up to three status webhooks; if the event volume is a
  problem a "has a status trigger" gate would be the next step. **Q-4**)
- **D-12 Callback declaration.** Per the coordinator's correction:
  `auth: {type: hub_signature, signature_header: X-Hub-Signature-256, secret_name: app_secret}`, methods
  `[GET, POST]`. The first idea (`meta_hub_signature` with `secret_ref` / `verification`) was replaced.
  Gate note: Core `main` (wt-triggers-stand) does not know `hub_signature`; the gate is run from the
  `codex/webhook-hub-signature` checkout (wt-wa-webhook).
- **D-13 Notifications.** `deliver_notification` sends plain text to a notify-capable contact; the 24-hour
  window error is surfaced as the delivery error. Approvals through buttons are OUT of scope for 0.1.0:
  no `human_decision` capability, no approval events subscribed, no interaction events returned. A button
  tap is delivered only as a normal `message_received` event (`button_id`, `button_title`). A generated
  notification longer than 4096 characters is shortened with an ellipsis (a workflow step is never
  shortened; it is refused).
- **D-14 No registration call.** The webhook is registered in the Meta dashboard (the `subscribed_apps`
  call is not documented on the pages I read), so the Setup page has a checklist and the status action only
  reads Flow Steward's own state (registration, last inbound time). The verify token is shown by Core in
  the callback registration.
- **D-15 `phone_number_id` is required by the UI, not by the connection schema**, so the connection can
  be created first by saving the token (the platform secret-save creates it with an empty config). Steps
  fail with `phone_number_id_required` until it is set.
- **D-16 Rate limit declarations** in `extension.yaml` (60 / 30 / 20 per minute) are Flow Steward-side
  throttles copied from the Telegram bundle, NOT Meta's limits (Meta: 80 mps per number, 250/2,000/... unique
  users per day outside the window).
- **D-17 Removed, not stubbed:** edit, delete, pin, unpin, polling, `inbound_mode`, webhook configuration
  action, approval/HITL token code, message formats (HTML), disable_notification, inline keyboards.
- **D-18 Locales.** `locales/en.yaml` is complete (generated from the manifests); `locales/lt.yaml` covers
  names, titles, labels and buttons only (long descriptions fall back to English). The Lithuanian is my
  own draft and needs a native review. **Q-5**
- **D-19 Icon.** `icon.png` is still the Flow Steward wave mark inherited from the snapshot (it is not
  Telegram branding). No image library was available here to draw a WhatsApp-style bubble, and copying
  WhatsApp's logo is a trademark matter. A real icon is needed before the catalog release. **Q-3**
- **D-20 Test hooks.** Unlike the Telegram bundle there is no `token.startswith("fake")` shortcut in the
  production code; tests mock the one transport function and a guard fixture fails any test that reaches
  the network.
- **D-21 Validator constraints that shaped the manifest.** (a) All external effects must share one
  `observability.target_kind`, so `mark_as_read` (which has no contact) is declared with `target_kind:
  channel`, `target_fields: [message_id]`, label "WhatsApp message". (b) The `ingress_event` feature requires
  `event_subscriptions` to include `external.comment.received`; it is subscribed (as in Telegram) although
  0.1.0 has no use for it: such an event would be delivered like any other notification if a route asks.

## Open questions for the owner

- **Q-1** Which host serves the media download URL? (`lookaside.fbsbx.com` is assumed.) Confirm on a real
  account and adjust `egress.hosts` and `MEDIA_HOST_SUFFIXES`.
- **Q-2** Minimum Core version: needs the `hub_signature` callback auth (branch codex/webhook-hub-signature)
  and provider-event triggers. README carries a TODO.
- **Q-3** Icon (see D-19).
- **Q-4** Should status events be emitted only when a status trigger exists (volume)?
- **Q-5** Native review of the Lithuanian text.
- **Q-6** Template header/button parameters and named parameters: wanted for 0.2? Needs reading Meta's
  template-components page.
- **Q-7** Groups (`recipient_type: group`) are not supported.
- **Q-8** A real end-to-end check against Meta (sandbox number) has NOT been done; everything is verified
  against mocked transport and documentation only.
