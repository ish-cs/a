# INSTRUCTIONS
# READ FULL FILE 

Build a full-stack real-time voice conversation web app. The user talks to an AI through their browser, and the AI can interrupt them if they speak too long.

## Stack
- Backend: FastAPI + WebSockets (Python)
- Frontend: Single HTML file with vanilla JS (served by FastAPI)
- STT: OpenAI Whisper API (gpt-4o-transcribe)
- LLM: Anthropic Claude claude-haiku-4-5-20251001 (streaming)
- TTS: MiniMax Speech 2.6 Turbo (POST https://api.minimax.io/v1/t2a_v2)
- Audio capture: WebAudio API in browser, streamed over WebSocket as PCM chunks

## Architecture
Single WebSocket connection between browser and server handles everything:
- Browser streams raw PCM audio chunks (16kHz, mono, 16-bit) to server continuously while user is speaking
- Server accumulates audio and runs a speech duration timer
- When user stops speaking (500ms silence) OR has been speaking for > 8 seconds (interrupt threshold), server processes the audio
- Server sends transcript, LLM response text, and TTS audio back to client over the same WebSocket as typed JSON messages + binary audio frames

## Message Protocol (WebSocket)
Client → Server:
  - Binary frames: raw PCM audio chunks
  - JSON: { type: "start_speaking" } when mic activates
  - JSON: { type: "stop_speaking" } when VAD detects silence
  - JSON: { type: "set_prompt", prompt: "..." } to update system prompt

Server → Client:
  - JSON: { type: "transcript", text: "..." }
  - JSON: { type: "llm_response", text: "..." }
  - JSON: { type: "interrupt" } — tells browser to stop recording and play AI response
  - JSON: { type: "audio_start" }
  - Binary frames: raw PCM audio from MiniMax TTS (streamed)
  - JSON: { type: "audio_end" }
  - JSON: { type: "status", message: "..." }

## Interruption Logic (backend)
- Start a timer when { type: "start_speaking" } is received
- If 8 seconds elapse and user is still speaking:
  - Stop accumulating audio
  - Send { type: "interrupt" } to client
  - Process whatever audio was collected so far (truncate + transcribe)
  - Generate AI response referencing the system prompt context as reason for interruption
  - Stream TTS back

## Conversation Memory
- Maintain full message history per WebSocket session (list of {role, content} dicts)
- System prompt: configurable via UI, default: "You are a friendly and engaging conversational AI. Keep responses natural and concise — 2-4 sentences max. If the user has been rambling for a while, gently redirect them."
- Pass full history to Claude on every turn

## Frontend UI (single index.html, served at /)
Design a clean, minimal dark-mode chat interface:
- Large circular mic button in center — hold to talk, release to send (also auto-releases on AI interrupt)
- Visual waveform animation while user is speaking (use WebAudio AnalyserNode)
- Chat transcript area showing alternating user/AI messages with smooth scroll
- Editable system prompt textarea at top (collapsible)
- Status indicator ("Listening...", "Thinking...", "Speaking...")
- AI response text appears as it streams in from LLM
- When AI audio plays, animate a speaker icon
- Mobile friendly

## Audio Pipeline Details
Browser mic capture:
  - getUserMedia({ audio: { sampleRate: 16000, channelCount: 1 } })
  - ScriptProcessorNode or AudioWorklet to capture raw PCM Float32
  - Downsample to 16kHz Int16 if needed
  - Send as binary WebSocket frames every 100ms

Backend audio assembly:
  - Accumulate Int16 PCM chunks into a buffer
  - On stop_speaking or interrupt: write buffer to temp WAV file (use wave module, 16kHz mono 16-bit)
  - Send WAV to Whisper API, get transcript
  - Clear buffer

MiniMax TTS:
  - POST to https://api.minimax.io/v1/t2a_v2 with voice_id "English_Expressive_Speaker", stream: true
  - Stream audio chunks back to browser as binary WebSocket frames
  - Browser decodes and plays using Web Audio API AudioContext

## File Structure
/app
  main.py         # FastAPI app, WebSocket handler, all backend logic
  requirements.txt
  static/
    index.html    # Entire frontend

## Env Vars
ANTHROPIC_API_KEY
OPENAI_API_KEY
MINIMAX_API_KEY
MINIMAX_GROUP_ID

## Error Handling
- If any API call fails, send { type: "status", message: "Error: ..." } and loop back to listening state
- Handle WebSocket disconnects cleanly, clear session memory
- Never crash the server on a bad audio frame

## Run Instructions
Add a comment block at top of main.py with:
pip install -r requirements.txt
uvicorn main:app --reload --port 8000