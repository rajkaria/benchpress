import { useId, useState, type FormEvent } from "react";

export type KeyPromptProps = {
  onSubmit: (key: string) => void;
  error?: string | null;
};

/** Shown whenever a call throws `Unauthorized`. Gateway-only: `benchpress ui` never gets a 401. */
export function KeyPrompt({ onSubmit, error }: KeyPromptProps) {
  const [key, setKey] = useState("");
  const inputId = useId();

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = key.trim();
    if (trimmed) onSubmit(trimmed);
  }

  return (
    <div className="key-prompt">
      <form onSubmit={handleSubmit}>
        <p>This gateway requires an API key.</p>
        <label htmlFor={inputId}>API key</label>
        <input
          id={inputId}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={key}
          onChange={(event) => setKey(event.target.value)}
        />
        <button type="submit">Continue</button>
        {error && (
          <p className="key-prompt-error" role="alert">
            {error}
          </p>
        )}
      </form>
    </div>
  );
}
