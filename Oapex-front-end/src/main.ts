import './style.css'

/* ------------------------------------------------------------------ */
/* Types mirroring the backend's /api/models response                   */
/* ------------------------------------------------------------------ */

interface ModelCapabilities {
  supports_voice_cloning?: boolean
  supports_multiple_audio_references?: boolean
  supports_image_reference?: boolean
  pricing?: Record<string, unknown>
}

interface SpeechModel {
  id: string
  name: string
  context_length?: number | null
  pricing?: Record<string, unknown>
  capabilities: ModelCapabilities
  known_voices: string[]
  provider_slug: string
}

interface ModelsResponse {
  models: SpeechModel[]
}

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

let models: SpeechModel[] = []
let selectedModel: SpeechModel | null = null
let currentAudioUrl: string | null = null
let currentExtension = 'mp3'

const API_KEY_STORAGE = 'oaepex.apiKey'

/* ------------------------------------------------------------------ */
/* App shell                                                           */
/* ------------------------------------------------------------------ */

const app = document.querySelector<HTMLDivElement>('#app')!

app.innerHTML = `
  <main class="shell">
    <header class="masthead">
      <h1>Oaepex</h1>
      <p class="tagline">OpenRouter Audio Endpoints — text to speech, simply.</p>
    </header>

    <section class="card" aria-labelledby="key-heading">
      <h2 id="key-heading">1. API key</h2>
      <label for="api-key">OpenRouter API key</label>
      <div class="key-row">
        <input id="api-key" type="password" autocomplete="off"
               placeholder="sk-or-v1-…" aria-describedby="key-help" />
        <button id="save-key" type="button">Save key</button>
      </div>
      <p id="key-help" class="hint">
        Stored only in this browser (localStorage) and sent only to your local
        backend, which forwards it to OpenRouter. Never written to disc.
      </p>
    </section>

    <section class="card" aria-labelledby="models-heading">
      <h2 id="models-heading">2. Choose a model</h2>
      <label for="model-select">Speech model</label>
      <select id="model-select" aria-describedby="model-status">
        <option value="">Loading models…</option>
      </select>
      <p id="model-status" role="status" aria-live="polite" class="hint"></p>
      <div id="model-caps" class="caps" aria-live="polite"></div>
    </section>

    <section class="card" aria-labelledby="text-heading">
      <h2 id="text-heading">3. Write your text</h2>
      <label for="text-input">Text to synthesize</label>
      <textarea id="text-input" rows="6"
        placeholder="Hello! This is a text-to-speech test."></textarea>
    </section>

    <section class="card" aria-labelledby="params-heading">
      <h2 id="params-heading">4. Parameters</h2>

      <div id="voice-group">
        <label for="voice-select">Voice</label>
        <select id="voice-select" aria-describedby="voice-help">
          <option value="">Provider default (if any)</option>
        </select>
        <input id="voice-custom" type="text" class="hidden"
               placeholder="Custom voice identifier" aria-label="Custom voice identifier" />
        <p id="voice-help" class="hint">
          Voices vary by model. If a reference file is chosen below, it is used
          instead of this voice — never both.
        </p>
      </div>

      <div id="instructions-group">
        <label for="instructions-input">Delivery instructions <span class="optional">(optional)</span></label>
        <input id="instructions-input" type="text"
               placeholder="Speak in a warm and friendly tone" />
      </div>

      <div class="param-grid">
        <div>
          <label for="format-select">Response format</label>
          <select id="format-select">
            <option value="mp3">mp3</option>
            <option value="pcm">pcm</option>
          </select>
        </div>
        <div>
          <label for="speed-input">Speed <span class="optional">(1.0)</span></label>
          <input id="speed-input" type="number" step="0.1" min="0.5" max="2" value="1" />
        </div>
      </div>

      <div id="clone-group" class="hidden">
        <label for="clone-file">Voice clone reference (WAV / MP3)</label>
        <input id="clone-file" type="file" accept="audio/wav,audio/mpeg,.wav,.mp3" />
        <label for="clone-transcript" class="sub-label">Reference transcript <span class="optional">(optional)</span></label>
        <input id="clone-transcript" type="text" placeholder="What the reference audio says" />
        <button id="clear-clone" type="button" class="ghost">Clear reference file</button>
      </div>
    </section>

    <section class="card" aria-labelledby="generate-heading">
      <h2 id="generate-heading">5. Generate</h2>
      <button id="generate" type="button" class="primary">Generate audio</button>
      <div id="status" role="status" aria-live="assertive" class="status"></div>

      <div id="result" class="hidden">
        <h3>Result</h3>
        <audio id="audio-player" controls></audio>
        <button id="download" type="button" class="primary">Download audio</button>
      </div>
    </section>

    <footer class="hint">
      Audio is played and kept in memory only. Nothing is saved to disc unless
      you press “Download audio”.
    </footer>
  </main>
`

/* ------------------------------------------------------------------ */
/* Element references                                                  */
/* ------------------------------------------------------------------ */

const apiKeyInput = document.querySelector<HTMLInputElement>('#api-key')!
const saveKeyBtn = document.querySelector<HTMLButtonElement>('#save-key')!
const modelSelect = document.querySelector<HTMLSelectElement>('#model-select')!
const modelStatus = document.querySelector<HTMLParagraphElement>('#model-status')!
const modelCaps = document.querySelector<HTMLDivElement>('#model-caps')!
const textInput = document.querySelector<HTMLTextAreaElement>('#text-input')!
const voiceGroup = document.querySelector<HTMLDivElement>('#voice-group')!
const voiceSelect = document.querySelector<HTMLSelectElement>('#voice-select')!
const voiceCustom = document.querySelector<HTMLInputElement>('#voice-custom')!
const instructionsInput = document.querySelector<HTMLInputElement>('#instructions-input')!
const formatSelect = document.querySelector<HTMLSelectElement>('#format-select')!
const speedInput = document.querySelector<HTMLInputElement>('#speed-input')!
const cloneGroup = document.querySelector<HTMLDivElement>('#clone-group')!
const cloneFile = document.querySelector<HTMLInputElement>('#clone-file')!
const cloneTranscript = document.querySelector<HTMLInputElement>('#clone-transcript')!
const clearClone = document.querySelector<HTMLButtonElement>('#clear-clone')!
const generateBtn = document.querySelector<HTMLButtonElement>('#generate')!
const statusEl = document.querySelector<HTMLDivElement>('#status')!
const resultEl = document.querySelector<HTMLDivElement>('#result')!
const audioPlayer = document.querySelector<HTMLAudioElement>('#audio-player')!
const downloadBtn = document.querySelector<HTMLButtonElement>('#download')!

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function setStatus(message: string, kind: 'info' | 'error' | 'success' = 'info'): void {
  statusEl.textContent = message
  statusEl.className = `status ${kind}`
}

function getApiKey(): string {
  return apiKeyInput.value.trim()
}

function authHeaders(): Record<string, string> {
  const key = getApiKey()
  return key ? { 'X-OpenRouter-Key': key } : {}
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init)
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail)
  }
  return resp.json() as Promise<T>
}

function fileToDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(new Error('Could not read the reference file.'))
    reader.readAsDataURL(file)
  })
}

/* ------------------------------------------------------------------ */
/* API key persistence                                                 */
/* ------------------------------------------------------------------ */

apiKeyInput.value = localStorage.getItem(API_KEY_STORAGE) ?? ''

saveKeyBtn.addEventListener('click', () => {
  localStorage.setItem(API_KEY_STORAGE, apiKeyInput.value.trim())
  setStatus('API key saved in this browser.', 'success')
})

/* ------------------------------------------------------------------ */
/* Model loading                                                       */
/* ------------------------------------------------------------------ */

function populateVoiceOptions(voices: string[]): void {
  voiceSelect.innerHTML = '<option value="">Provider default (if any)</option>'
  for (const v of voices) {
    const opt = document.createElement('option')
    opt.value = v
    opt.textContent = v
    voiceSelect.appendChild(opt)
  }
  const custom = document.createElement('option')
  custom.value = '__custom__'
  custom.textContent = 'Custom…'
  voiceSelect.appendChild(custom)
}

function updateModelCaps(model: SpeechModel | null): void {
  modelCaps.innerHTML = ''
  cloneGroup.classList.add('hidden')
  if (!model) return

  const caps = model.capabilities ?? {}
  const badges: string[] = []
  if (caps.supports_voice_cloning) badges.push('voice cloning')
  if (caps.supports_multiple_audio_references) badges.push('multiple references')
  if (caps.supports_image_reference) badges.push('image reference')
  if (caps.pricing?.prompt) {
    badges.push(`$${Number(caps.pricing.prompt).toPrecision(3)} / char`)
  }

  modelCaps.textContent = badges.length
    ? `Capabilities: ${badges.join(' · ')}`
    : 'No special capabilities reported for this model.'

  if (caps.supports_voice_cloning) {
    cloneGroup.classList.remove('hidden')
  }
}

function onModelChange(): void {
  const id = modelSelect.value
  selectedModel = models.find((m) => m.id === id) ?? null
  populateVoiceOptions(selectedModel?.known_voices ?? [])
  voiceCustom.classList.add('hidden')
  updateModelCaps(selectedModel)
}

modelSelect.addEventListener('change', onModelChange)

voiceSelect.addEventListener('change', () => {
  voiceCustom.classList.toggle('hidden', voiceSelect.value !== '__custom__')
})

cloneFile.addEventListener('change', () => {
  // A reference file replaces the voice — never pass both.
  const hasFile = cloneFile.files !== null && cloneFile.files.length > 0
  voiceGroup.classList.toggle('hidden', hasFile)
  if (hasFile) {
    voiceSelect.value = ''
  }
})

clearClone.addEventListener('click', () => {
  cloneFile.value = ''
  cloneTranscript.value = ''
  voiceGroup.classList.remove('hidden')
})

async function loadModels(): Promise<void> {
  modelSelect.disabled = true
  modelStatus.textContent = 'Loading available speech models…'
  try {
    const data = await fetchJson<ModelsResponse>('/api/models', { headers: authHeaders() })
    models = data.models
    modelSelect.innerHTML = ''
    for (const m of models) {
      const opt = document.createElement('option')
      opt.value = m.id
      opt.textContent = m.name
      modelSelect.appendChild(opt)
    }
    modelSelect.disabled = false
    modelStatus.textContent = `${models.length} speech model${models.length === 1 ? '' : 's'} available.`
    onModelChange()
  } catch (err) {
    modelSelect.innerHTML = '<option value="">No models available</option>'
    modelStatus.textContent = err instanceof Error ? err.message : String(err)
    modelStatus.className = 'hint error'
  }
}

/* ------------------------------------------------------------------ */
/* Generation                                                          */
/* ------------------------------------------------------------------ */

function currentVoice(): string | undefined {
  if (cloneFile.files && cloneFile.files.length > 0) return undefined
  if (voiceSelect.value === '__custom__') {
    const v = voiceCustom.value.trim()
    return v || undefined
  }
  return voiceSelect.value || undefined
}

async function buildInputReferences(): Promise<Array<Record<string, unknown>> | undefined> {
  if (!cloneFile.files || cloneFile.files.length === 0) return undefined
  const file = cloneFile.files[0]
  const dataUri = await fileToDataUri(file)
  const ref: Record<string, unknown> = {
    type: 'input_audio',
    input_audio: { data: dataUri },
  }
  const transcript = cloneTranscript.value.trim()
  if (transcript) {
    return [ref, { type: 'text', text: transcript }]
  }
  return [ref]
}

generateBtn.addEventListener('click', async () => {
  if (!selectedModel) {
    setStatus('Choose a model first.', 'error')
    return
  }
  const text = textInput.value.trim()
  if (!text) {
    setStatus('Enter some text to synthesize.', 'error')
    return
  }
  if (!getApiKey()) {
    setStatus('Enter your OpenRouter API key first.', 'error')
    return
  }

  generateBtn.disabled = true
  setStatus('Generating audio…', 'info')

  try {
    const inputReferences = await buildInputReferences()
    const speed = Number(speedInput.value)

    const payload: Record<string, unknown> = {
      model: selectedModel.id,
      input: text,
      response_format: formatSelect.value,
    }
    const voice = currentVoice()
    if (voice) payload.voice = voice
    const instructions = instructionsInput.value.trim()
    if (instructions) payload.instructions = instructions
    if (!Number.isNaN(speed) && speed !== 1) payload.speed = speed
    if (inputReferences) payload.input_references = inputReferences

    const resp = await fetch('/api/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    })

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const body = await resp.json()
        if (body?.detail) detail = body.detail
      } catch {
        /* not JSON */
      }
      throw new Error(detail)
    }

    const blob = await resp.blob()
    if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl)
    currentAudioUrl = URL.createObjectURL(blob)
    currentExtension = formatSelect.value === 'pcm' ? 'pcm' : 'mp3'

    audioPlayer.src = currentAudioUrl
    resultEl.classList.remove('hidden')
    const genId = resp.headers.get('X-Generation-Id')
    setStatus(
      `Audio ready (${(blob.size / 1024).toFixed(1)} KiB)` +
        (genId ? ` — generation ID: ${genId}` : '') +
        '. Press play, or download it.',
      'success',
    )
  } catch (err) {
    setStatus(err instanceof Error ? err.message : String(err), 'error')
  } finally {
    generateBtn.disabled = false
  }
})

downloadBtn.addEventListener('click', () => {
  if (!currentAudioUrl) return
  const a = document.createElement('a')
  a.href = currentAudioUrl
  a.download = `oaepex-audio.${currentExtension}`
  a.click()
})

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

void loadModels()
