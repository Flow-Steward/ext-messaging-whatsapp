# Workflow actions

Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts.

Select the connection in the workflow step. Map returned message IDs, file handles or dataset handles into later steps rather than pasting credentials or local paths.

## Send WhatsApp message (`send_message`)

Send a text message to one of this connection's WhatsApp contacts, optionally with up to 3 reply buttons. Text is at most 4096 characters, at most 1024 with buttons; a longer text is refused, never cut. Outside the 24-hour window WhatsApp refuses it: use Send template. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Send a processing confirmation to a saved contact during the active 24-hour messaging window.

Inputs:

- `channel_id`: The ID of a contact on the Contacts page. May be left empty when only one contact is enabled for notifications.
- `wa_id`: Alternative to the contact ID: the phone number (digits only) of a saved contact, for example the wa_id of a New WhatsApp message trigger.
- `text`: The text to send, at most 4096 characters (1024 with buttons). A Template step's result fits here.
- `preview_url`: Show a preview of the first URL (it must start with http:// or https://).
- `reply_to_message_id`: Send as a reply to this WhatsApp message ID (a wamid). Leave empty for a normal message.
- `buttons`: Up to 3 reply buttons: a list of {"id": "yes", "title": "Yes"}. A title is at most 20 characters and unique; an id is at most 256 characters and unique. With buttons the message is interactive and its text is at most 1024 characters.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Send WhatsApp template (`send_template`)

Send an approved message template to one of this connection's WhatsApp contacts. Templates are the only messages WhatsApp allows outside the 24-hour window. Only positional body text parameters are supported. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Send an approved order_update template with body_parameters=["A-100","shipped"] outside the 24-hour messaging window.

Inputs:

- `channel_id`: The ID of a contact on the Contacts page. May be left empty when only one contact is enabled for notifications.
- `wa_id`: Alternative to the contact ID: the phone number (digits only) of a saved contact, for example the wa_id of a New WhatsApp message trigger.
- `template_name`: The name of an APPROVED message template.
- `language`: The template's language code, for example en_US or lt.
- `body_parameters`: The texts for the template body placeholders in order, for example ["Jessica", "12"].

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Send WhatsApp reaction (`send_reaction`)

React with an emoji to a message a contact sent you. The message must be at most 30 days old and must be one you received. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Acknowledge an incoming message with a checkmark emoji using the trigger's message_id.

Inputs:

- `channel_id`: The ID of a contact on the Contacts page. May be left empty when only one contact is enabled for notifications.
- `wa_id`: Alternative to the contact ID: the phone number (digits only) of a saved contact, for example the wa_id of a New WhatsApp message trigger.
- `message_id`: The ID (wamid) of the received message to react to, for example message_id of a New WhatsApp message trigger.
- `emoji`: The emoji to react with.

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Mark WhatsApp message as read (`mark_as_read`)

Mark a received message as read (the sender sees blue ticks). Works within 30 days of receipt; it also marks earlier messages of the chat as read. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Mark an incoming customer message as read after the intake workflow records it.

Inputs:

- `message_id`: The ID (wamid) of the received message, for example message_id of a New WhatsApp message trigger.

Result fields: `marked_read`, `message_id`, `external_effect_status`.

## Send WhatsApp file (`send_file`)

Send a file from the workflow to one of this connection's WhatsApp contacts as an image, document, audio or video. Limits: image 5 MB (jpeg, png), document 100 MB (txt, xls, xlsx, doc, docx, ppt, pptx, pdf), audio 16 MB, video 16 MB (3gp, mp4). A larger file is refused, never cut. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Send a generated invoice PDF artifact as kind=document to a saved contact.

Inputs:

- `channel_id`: The ID of a contact on the Contacts page. May be left empty when only one contact is enabled for notifications.
- `wa_id`: Alternative to the contact ID: the phone number (digits only) of a saved contact, for example the wa_id of a New WhatsApp message trigger.
- `kind`: How WhatsApp shows the file: image, document, audio or video.
- `artifact_handle`: The file (an artifact) to send, for example one a Fetch File or Get WhatsApp file step produced.
- `caption`: Optional text under an image, video or document, at most 1024 characters. Audio has no caption.
- `file_name`: The name WhatsApp shows for a document. Defaults to the artifact's name; include the extension.
- `mime_type`: Override the file's MIME type, for example application/pdf. Defaults to the artifact's type or its file extension.
- `reply_to_message_id`: Send as a reply to this WhatsApp message ID (a wamid).

Result fields: `delivered`, `provider_message_id`, `channel_id`, `external_effect_status`.

## Download WhatsApp file (`get_file`)

Download a received file, by its WhatsApp media ID, into a workflow artifact. The download link lives 5 minutes and media IDs from webhooks are kept 7 days, so run the step soon after the message arrives. A file over 100 MB is refused, never cut. Create a WhatsApp Business Cloud API project connection with its access token and phone number ID, then add and enable the recipient on Contacts. Example: Download the file_id from a New WhatsApp message trigger and pass the artifact to extraction.

Inputs:

- `file_id`: The file_id of a New WhatsApp message trigger (the WhatsApp media ID).
- `file_name`: Optional name for the artifact. Defaults to whatsapp_<media id> with an extension from the file type.

Result fields: `artifact_handle`, `artifact_filename`, `mime_type`, `size_bytes`, `sha256`.
