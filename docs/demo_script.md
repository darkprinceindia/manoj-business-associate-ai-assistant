# Demo walkthrough and narration

Target length: about 4-6 minutes. Record the browser at a readable scale and keep passwords and API keys off screen.

| Scene | On-screen action | Suggested narration |
| --- | --- | --- |
| 1. Sign-in and profile preview | Open Create a profile, show the name, email, and phone fields, then return to sign-in. | “The profile page is a visual preview. It does not send details or create an account yet.” |
| 2. IFB knowledge bank | Sign in as Customer and open Knowledge library. | “This local demo has manufacturer references for two IFB AC models and IFB's warranty conditions.” |
| 3. Cited answer | Ask “What warranty does the IFB CI1832D223GN1 3-star AC have?” and open a citation. | “The answer comes from IFB's official model page and terms; the conditions and purchase date still matter.” |
| 4. No-answer path | Ask for the price of a specific laptop model. | “No shop document supports a laptop price, so the assistant asks the customer to confirm with the shop.” |
| 5. Ingestion | Sign in as Admin. Open Knowledge library and upload a verified shop guide or PDF with Public visibility. | “The backend extracts text, splits it into chunks, creates embeddings, and stores them in Qdrant. SQLite keeps the document and its searchable text.” |
| 6. Product route | Ask a product question about the guide. Open Connectors. | “Qdrant finds nearby chunks. Product questions also go through a read-only MCP tool. The backend checks visibility before answering.” |
| 7. Chat history and Dashboard | Reopen the thread under Your Chats, rate an answer, then sign in as Admin to mark it Correct or Incorrect. | “SQLite saves the chat. The Dashboard separates customer feedback from Admin-reviewed accuracy.” |

## Narration options

The written script is ready to read in your own voice. **No voice sample is required** for that. If you later want an AI-generated narration that resembles your own voice, provide a clean recording and explicitly request that separate production step; use only a voice you are authorized to reproduce. A standard synthetic narrator also needs no sample. The app itself does not contain a voice-cloning feature.
