import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { workspaceApi, type Scenario } from "./api";
import { Icon } from "./Icons";

export function ImportDialog({
  parent,
  onClose,
  onImported,
}: {
  parent?: Scenario;
  onClose: () => void;
  onImported: (scenario: Scenario) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(
    parent ? `${parent.name} · alternative` : "",
  );
  const [content, setContent] = useState(
    parent ? JSON.stringify(parent.capsule, null, 2) : "",
  );
  const [fileError, setFileError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const mutation = useMutation({
    mutationFn: () =>
      parent
        ? workspaceApi.branch(parent.id, name, content)
        : workspaceApi.import(name, content),
    onSuccess: onImported,
  });
  async function readFile(file?: File) {
    if (!file) return;
    if (file.size > 5_242_880) {
      setFileError(
        "This file is too large. Import a scenario smaller than 5 MB.",
      );
      return;
    }
    try {
      setContent(await file.text());
      if (!name)
        setName(file.name.replace(/\.twin\.(yaml|json)$|\.(yaml|json)$/g, ""));
      setFileError("");
    } catch {
      setFileError("Could not read this file. Try selecting it again.");
    }
  }
  return (
    <dialog ref={dialog} className="ws-dialog" onCancel={onClose}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <div className="ws-dialog-heading">
          <div>
            <span className="ws-eyebrow">
              {parent ? "BRANCH A SCENARIO" : "BRING YOUR OPERATION"}
            </span>
            <h2>
              {parent
                ? "Explore another possibility."
                : "One file. A working twin."}
            </h2>
          </div>
          <button
            type="button"
            className="ws-icon-button"
            onClick={onClose}
            aria-label="Close import"
          >
            <Icon name="close" />
          </button>
        </div>
        <p>
          {parent
            ? "Edit the scenario below. Your baseline is preserved, and every change is validated before a new version is created."
            : "Import a Twinflow scenario capsule (.twin.yaml or .twin.json). Your processes, demand, and assumptions travel together. Every example folder ships one, such as examples/spring/spring.twin.yaml. A bare model.yaml is a floor model, not a capsule; convert it with twinflow scenario import."}
        </p>
        {!parent && (
          <label
            className="ws-dropzone"
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              void readFile(event.dataTransfer.files[0]);
            }}
          >
            <Icon name="upload" size={28} />
            <strong>Drop your scenario here</strong>
            <span>
              or choose a file · .twin.yaml or .twin.json · up to 5 MB
            </span>
            <input
              type="file"
              accept=".yaml,.yml,.json"
              aria-label="Choose scenario file"
              onChange={(event) => void readFile(event.target.files?.[0])}
            />
          </label>
        )}
        <label className="ws-field">
          Scenario name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={160}
            placeholder="My operation"
          />
        </label>
        <label className="ws-field">
          {parent ? "Scenario content" : "Or paste scenario content"}
          <textarea
            className="ws-source-input"
            value={content}
            onChange={(event) => setContent(event.target.value)}
            required
            spellCheck={false}
            placeholder={'schema_version: "0.1"\nmodel: …\nsnapshot: …'}
          />
        </label>
        {(fileError || mutation.error) && (
          <p className="ws-error" role="alert">
            {fileError || mutation.error?.message}
          </p>
        )}
        <footer>
          <button type="button" className="ws-button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="ws-button primary"
            disabled={mutation.isPending || !content || !name}
          >
            {mutation.isPending
              ? "Validating…"
              : parent
                ? "Create alternative"
                : "Validate & import"}
            <Icon name="arrow" size={16} />
          </button>
        </footer>
      </form>
    </dialog>
  );
}
