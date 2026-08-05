/**
 * Dictation for the Ask box, using only what the browser already ships.
 *
 * No dependency, no recording, no upload. `SpeechRecognition` is the browser's
 * own API and whatever it does with the audio is its arrangement with its
 * vendor — Chrome relays to a Google service, Safari does its own thing on
 * device. Nothing here captures a stream, and no audio is sent to this
 * application's server at any point. That is worth stating plainly, because
 * "voice input" in a product usually means the opposite.
 *
 * IT MUST DEGRADE, BECAUSE HALF THE BROWSERS CANNOT DO THIS
 * ---------------------------------------------------------
 * Firefox has no speech recognition at all and Safari's is partial. So this
 * hook reports what it has rather than assuming: `supported` is false when the
 * constructor is missing, and the screen is expected to draw a plain text box
 * with no microphone beside it. A microphone button that does nothing when
 * pressed is worse than no microphone button, because the person keeps
 * pressing it.
 *
 * A BLOCKED MICROPHONE IS NOT A MISSING MICROPHONE
 * ------------------------------------------------
 * The two get one message between them in most implementations, and it is the
 * wrong message for both readers: one person needs to know their browser will
 * never do this, and the other needs to know they can fix it in the address
 * bar in two seconds. So `problem.kind` tells them apart and each carries its
 * own sentence.
 */

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The slice of the Web Speech API this hook touches, declared locally.
 *
 * Module-scoped on purpose: `lib.dom` may or may not carry these depending on
 * the TypeScript release, and `webkitSpeechRecognition` is not in it under any
 * release. Declaring them globally would collide the day the DOM library
 * catches up; declaring them here cannot.
 */
interface SpeechAlternative {
  readonly transcript: string;
}

interface SpeechResult {
  readonly [index: number]: SpeechAlternative;
}

interface SpeechResultList {
  readonly length: number;
  readonly [index: number]: SpeechResult;
}

interface SpeechResultEvent {
  readonly results: SpeechResultList;
}

interface SpeechErrorEvent {
  /** One of the codes in the Web Speech spec — `not-allowed`, `network`, … */
  readonly error: string;
}

interface Recogniser {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onerror: ((event: SpeechErrorEvent) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type RecogniserConstructor = new () => Recogniser;

/**
 * Chrome, Edge and Safari expose it under the webkit prefix; the unprefixed
 * name is the one the spec settled on. Read once, at module load, so the
 * screen can decide whether to draw a microphone before anyone presses it.
 */
const RECOGNISER: RecogniserConstructor | null = (() => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecogniserConstructor;
    webkitSpeechRecognition?: RecogniserConstructor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
})();

/**
 * Indian English, because the words being dictated are Indian.
 *
 * Branch names, distributor names and half the catalogue are proper nouns that
 * `en-US` mangles beyond recognition — it hears "Andheri" as "and Harry" and
 * "Dolo" as "dollar". The recogniser is still going to get drug names wrong,
 * which is why nothing it produces is ever submitted without a person reading
 * it; this just makes the corrections shorter.
 */
const DICTATION_LANGUAGE = "en-IN";

export type VoiceProblemKind =
  | "unsupported"
  | "denied"
  | "no-microphone"
  | "network"
  | "failed";

/** Something the person needs told, and enough of a label to style it by. */
export interface VoiceProblem {
  kind: VoiceProblemKind;
  message: string;
}

const UNSUPPORTED: VoiceProblem = {
  kind: "unsupported",
  message:
    "This browser cannot listen — Firefox is the usual reason. Type the " +
    "question instead; nothing else on this screen changes.",
};

const FAILED: VoiceProblem = {
  kind: "failed",
  message: "The microphone stopped unexpectedly. Press it again, or type instead.",
};

/**
 * The error codes worth a sentence of their own.
 *
 * `not-allowed` is the person's own refusal and `service-not-allowed` is the
 * browser's — a policy or an insecure origin — and both are fixed in the same
 * place, so they read the same. Anything not listed falls through to `FAILED`,
 * which says what happened without pretending to know why.
 */
const PROBLEMS: Record<string, VoiceProblem> = {
  "not-allowed": {
    kind: "denied",
    message:
      "The microphone is blocked for this site. Allow it from the icon in " +
      "the address bar, then press the microphone again.",
  },
  "service-not-allowed": {
    kind: "denied",
    message:
      "The browser will not allow speech recognition here. Type the question " +
      "instead, or ask for the site to be opened over https.",
  },
  "audio-capture": {
    kind: "no-microphone",
    message: "No microphone was found. Type the question instead.",
  },
  network: {
    kind: "network",
    message:
      "This browser does its speech recognition over the network and could " +
      "not reach it. Type the question instead.",
  },
};

export interface Voice {
  /** False on a browser with no speech recognition — draw no microphone. */
  supported: boolean;
  listening: boolean;
  /** Set while unsupported, and after a failed or refused attempt. */
  problem: VoiceProblem | null;
  start: () => void;
  stop: () => void;
}

/**
 * Listen, and hand the words to `onHeard` as they arrive.
 *
 * `onHeard` receives the whole of the current dictation every time it grows,
 * not the newest fragment: interim results are revised as the recogniser hears
 * more of the sentence, so the caller replaces rather than appends. What the
 * caller does with the text is its own business — this hook holds no draft and
 * submits nothing, which is what keeps "the words went in the box" from
 * becoming "the words were sent".
 */
export function useVoice(onHeard: (text: string) => void): Voice {
  const [listening, setListening] = useState(false);
  const [problem, setProblem] = useState<VoiceProblem | null>(null);
  const session = useRef<Recogniser | null>(null);

  // Read through a ref so an unrelated re-render of the page cannot tear down
  // a recogniser in the middle of a sentence.
  const heard = useRef(onHeard);
  useEffect(() => {
    heard.current = onHeard;
  });

  const start = useCallback(() => {
    // Already listening. Starting a second recogniser throws in Chrome and
    // silently doubles every word in Safari.
    if (!RECOGNISER || session.current) return;

    const recogniser = new RECOGNISER();
    recogniser.lang = DICTATION_LANGUAGE;
    // The words have to appear while they are being said. Without this the box
    // stays empty for four seconds and the person says it again, louder.
    recogniser.interimResults = true;
    // Stops at the end of an utterance rather than staying open. A microphone
    // that keeps listening after the question has been asked collects the next
    // conversation in the shop, and one question is one utterance.
    recogniser.continuous = false;
    recogniser.maxAlternatives = 1;

    recogniser.onresult = (event) => {
      let sentence = "";
      for (let i = 0; i < event.results.length; i += 1) {
        sentence += event.results[i][0].transcript;
      }
      heard.current(sentence.trim());
    };

    recogniser.onerror = (event) => {
      // `aborted` is this hook stopping it, and `no-speech` is somebody who
      // pressed the button and then thought about the question. Telling them
      // about either is noise.
      if (event.error === "aborted" || event.error === "no-speech") return;
      setProblem(PROBLEMS[event.error] ?? FAILED);
    };

    recogniser.onend = () => {
      session.current = null;
      setListening(false);
    };

    try {
      recogniser.start();
    } catch {
      // Safari throws here instead of firing `onerror` — a previous session
      // still winding down, or a gesture it did not accept as one.
      setProblem(FAILED);
      return;
    }

    session.current = recogniser;
    setProblem(null);
    setListening(true);
  }, []);

  const stop = useCallback(() => {
    // `stop`, never `abort`: it delivers the last interim result as a final
    // one, so the last few words are not thrown away by pressing stop.
    session.current?.stop();
  }, []);

  useEffect(
    () => () => {
      // Leaving the screen switches the microphone off. A recogniser left
      // running is a live microphone on a page nobody is looking at, and its
      // callbacks would be writing into a box that no longer exists.
      const running = session.current;
      session.current = null;
      if (!running) return;
      running.onresult = null;
      running.onerror = null;
      running.onend = null;
      running.abort();
    },
    [],
  );

  return {
    supported: RECOGNISER !== null,
    listening,
    problem: RECOGNISER ? problem : UNSUPPORTED,
    start,
    stop,
  };
}
