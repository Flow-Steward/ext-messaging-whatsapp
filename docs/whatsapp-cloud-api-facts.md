# WhatsApp Cloud API facts used by this extension

Checked on 2026-10-28 against Meta's developer documentation (links per item).
Only facts a page states are listed. Where a page was silent, the item says so; do not
invent limits that are not here. Graph API version in Meta's examples: `v26.0`.

## Sending (POST https://graph.facebook.com/<VERSION>/<PHONE_NUMBER_ID>/messages)

- Auth: `Authorization: Bearer <access token>`, `Content-Type: application/json`.
  Source: .../whatsapp/cloud-api/guides/send-messages
- Common body fields: `messaging_product: "whatsapp"`, `recipient_type: "individual"` (a group
  is `"group"`), `to` (phone number), `type`.
- Success response (message id) means the request was ACCEPTED, not delivered; delivery arrives
  as `statuses` webhooks. Message order is not guaranteed. Default message TTL is 30 days.
- **Text:** `type: "text"`, `text: {body, preview_url?}`. `body` required, **max 4096
  characters**. `preview_url` previews only the first URL; URLs must start with http:// or
  https://. Source: .../messages/text-messages.md
- **Contextual reply:** add `context: {message_id: "<wamid>"}` beside `type`. A reaction cannot
  be a contextual reply. The quoted bubble does not show if the original is deleted/older than
  30 days, or if the reply is a template. Source: .../messages/contextual-replies.md
- **Reaction:** `type: "reaction"`, `reaction: {message_id, emoji}`. Target message must be
  <= 30 days old, in the same chat, not deleted, not itself a reaction, and one that you
  RECEIVED from a user. Failure arrives as a webhook with error code 131009. Removing a reaction
  is not documented. Source: .../messages/reaction-messages.md
- **Reply buttons:** `type: "interactive"`, `interactive: {type: "button", header?, body:
  {text}, footer?: {text}, action: {buttons: [{type: "reply", reply: {id, title}}]}}`.
  **Max 3 buttons; title <= 20 chars and unique; id <= 256 chars and unique; body text <= 1024
  chars (required); footer <= 60 chars.** Header types: document, image, text, video (media by
  uploaded `id`; `link` is discouraged). Source: .../messages/interactive-reply-buttons-messages.md
- **Mark as read:** POST same endpoint with `{messaging_product: "whatsapp", status: "read",
  message_id: "<wamid from an inbound webhook>"}`. Only within 30 days of receipt; invalid id
  -> error 131009. In an individual chat it also marks earlier messages read. Needs a system or
  business token. The typing indicator is NOT documented on that page.
  Source: .../messages/mark-message-as-read.md
- **Template:** `type: "template"`, `template: {name, language: {code}, components: [{type:
  "body", parameters: [{type: "text", text: "..."}]}]}`. Named parameters add `parameter_name`;
  positional ones omit it and follow placeholder order. The template must be `APPROVED`.
  Categories: authentication, marketing, utility. Source: .../templates/overview.md
  (the dedicated "send template" page returned 404; header/button parameter shapes were NOT
  verified).

## The 24-hour customer service window

- Opens when a user messages (or calls) you; lasts 24 h; resets on every new user message.
- Inside the window any supported message type may be sent. Outside it ONLY approved template
  messages. You may message only users who opted in. Source: .../cloud-api/guides/send-messages
- Business-initiated limits count unique users messaged outside a window per rolling 24 h per
  business portfolio: 250 default, 2,000 after scaling, then 10,000 / 100,000 / unlimited.
  Source: .../messaging-limits.md. Throughput (messages per second) and pair rate limits: the
  fetched page did NOT state them.

## Media (https://graph.facebook.com/<VERSION>/...)

- Upload: `POST /<PHONE_NUMBER_ID>/media`, multipart: `messaging_product=whatsapp`, `file`
  (with MIME type), `type` (required). Response `{"id": "<MEDIA_ID>"}`. Uploaded media persists
  30 days unless deleted. Source: .../business-phone-numbers/media.md
- Limits: audio 16 MB (aac, amr, mpeg, mp4, ogg only with OPUS mono); document 100 MB (txt,
  xls, xlsx, doc, docx, ppt, pptx, pdf); image 5 MB (jpeg, png; 8-bit RGB/RGBA); sticker webp
  (animated 500 KB, static 100 KB); video 16 MB (3gp, mp4; H.264 + AAC). A wrong MIME type is
  error 131053.
- Download: `GET /<MEDIA_ID>` (bearer; optional `phone_number_id`) -> `{url, mime_type, sha256,
  file_size, id}`. The `url` expires after **5 minutes**. Then `GET <url>` WITH the bearer token
  returns the bytes. 404 -> fetch a new url; if it still fails renew the token.
- Media id retention: ids returned by the API 30 days; ids received in webhooks **7 days**.
- Delete: `DELETE /<MEDIA_ID>` -> `{"success": true}`.

## Webhooks

- Verification (GET): `?hub.mode=subscribe&hub.challenge=<random>&hub.verify_token=<string you
  chose>`. Compare `hub.verify_token` with your stored string; if valid answer HTTP 200 with the
  `hub.challenge` value as the body, otherwise a non-200. Source: .../webhooks/create-webhook-endpoint.md
- Delivery (POST): header `X-Hub-Signature-256: sha256=<hex>` = HMAC-SHA256 of the RAW request
  body keyed with the **app secret** of the Meta app. Compare after the `sha256=` prefix.
  Answer HTTP 200. Failed deliveries are retried immediately and then for up to 7 days, so
  deduplicate. Payload up to 3 MB.
- Payload: `{object: "whatsapp_business_account", entry: [{id, changes: [{field: "messages",
  value: {messaging_product, metadata: {display_phone_number, phone_number_id}, contacts:
  [{profile: {name}, wa_id}], messages: [{from, id (wamid), timestamp, type, <type>: {...}}],
  statuses: [...]}}]}]}`. `text.body` for text. Button tap: `type: "interactive"`,
  `interactive: {type: "button_reply", button_reply: {id, title}}`. The page did not give the
  fields for image/document/audio/video/location/reaction messages - read each type's reference
  page before parsing them (Meta documents `image: {id, mime_type, sha256, caption}` and
  `document: {id, filename, mime_type, sha256, caption}` on the per-type pages; confirm before
  relying on it).
- Statuses: `value.statuses[]` with `id` (wamid), `status` in sent / delivered / read / failed
  (+ `played` for voice), `timestamp`, `recipient_id` (a phone number as digits; the group id
  for group messages), `conversation`, `pricing`. A `failed` status carries `errors[]` with
  `code`, `title`, `message`, `error_data.details`, `href`. Source: .../webhooks/reference/messages/status.md
- Errors can also arrive in `value.errors`, `messages[].errors` (type `unsupported`) and
  `statuses[].errors`.

## What the Cloud API does NOT offer (so the extension must not pretend to)

- No editing, deleting or pinning of messages already sent (these exist for Telegram only).
- No polling (`getUpdates`) equivalent: inbound traffic arrives ONLY by webhook. A deployment
  that Meta cannot reach (for example localhost) cannot receive messages.
- Free-form messages are refused outside the 24-hour window (a reply to the sender within
  the window is fine); use a template.

## Added on 2026-10-28 (second pass; each item names the Meta page that states it)

Base: https://developers.facebook.com/documentation/business-messaging/whatsapp/

- **Inbound media objects** (`webhooks/reference/messages/{image,video,document,audio}.md`):
  `image` and `video`: `{caption?, mime_type, sha256, id, url?}`; `document`: `{filename, caption?,
  mime_type, sha256, id, url?}`; `audio`: `{mime_type, sha256, id, url?, voice}` (`voice` true = recorded
  in the WhatsApp voice recorder; audio has no caption). `url` is being released gradually since
  2025-11-12 and may be absent; the extension always resolves `id` through `GET /<MEDIA_ID>`.
- **Location** (`.../location.md`): `location: {address, latitude (float), longitude (float), name, url?}`.
- **Reaction** (`.../reaction.md`): `reaction: {message_id, emoji}`; `emoji` is omitted when the user
  removes the reaction.
- **Interactive replies** (`.../interactive.md`): `interactive: {type: "button_reply", button_reply:
  {id, title}}` and `{type: "list_reply", list_reply: {id, title, description}}`. Replies carry
  `context: {from, id}`; for a button tap `context.id` is the message that held the button.
- **Sending media by id** (`messages/{image,document,audio,video}-messages.md`): `image: {id, caption?}`
  (caption max 1024), `video: {id, caption?}` (max 1024), `document: {id, caption?, filename?}`
  (caption max 1024; give the filename WITH extension), `audio: {id, voice?}` (no caption; voice
  messages must be OGG/OPUS). Audio mime must match the file extension.
- **Upload `type`** (`business-phone-numbers/media.md`): the multipart `type` is the file's MIME type
  (`type=<MIME_TYPE>`). Accepted types: audio aac, amr, mpeg, mp4, ogg(opus, mono); document txt, xls,
  xlsx, doc, docx, ppt, pptx, pdf; image jpeg, png; video 3gpp, mp4. The download URL host is NOT named
  on that page; a failed download is 404 (get a new URL, then renew the token).
- **Error codes** (`support/error-codes.md`): 4 app API rate limit (throttling); 80007 WABA rate limit;
  130429 throughput limit; 131056 too many messages to the same recipient (all four: wait, retry later);
  131047 more than 24 h since the recipient last replied (send a template); 131009 invalid parameter
  value; 131008 required parameter missing; 100 unsupported/misspelled parameter or length limit; 131053
  media upload failed (see `error_data.details`); 190 access token expired; 131026 undeliverable
  (non-WhatsApp number, terms not accepted, outdated client); 131021 sender equals recipient; 133010
  number not registered; 132000 template parameter count mismatch; 132001 template missing in that
  language or not approved; 132005 translated text too long; 132012 parameter format mismatch; 368 account
  restricted for a policy violation; 131016 service temporarily unavailable; 131042 payment problem;
  131051 unsupported message type; 131052 user's media could not be downloaded; 131000 unknown error.
  131030 was not covered by the page.
- **Throughput** (`throughput.md`): default 80 messages per second per business phone number (up to
  1,000 mps automatic upgrade; 20 mps for numbers also used by the Business app); exceeding it returns
  130429; the pair rate limit (same recipient) exists, its number is not stated.
- **Phone number read** (`business-phone-numbers/phone-numbers.md`): `GET /<API_VERSION>/<PHONE_NUMBER_ID>`
  returns `verified_name`, `display_phone_number`, `id`, `quality_rating` (extra fields via `?fields=`).
  The page does not document a `subscribed_apps` call, so the extension does not register webhooks.

Still NOT verified (the extension does not use them): template header and button parameter shapes (the
"template components" page was not read), sticker inbound fields, group messaging, the media download
host, the message-send `Retry-After` behaviour.
