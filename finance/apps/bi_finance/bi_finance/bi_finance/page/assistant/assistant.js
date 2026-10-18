// The Assistant desk page: a chat over the read-only collectors, see assistant.py.
//
// The page holds only the conversation: the question and the past turns go to
// bi_finance.assistant.ask, and what comes back is text plus the documents to link. The OpenRouter
// key never reaches the browser; the server holds it and the request alone. "What is open for me"
// runs the same call with the fixed question, so the daily opener needs no typing.

frappe.pages["assistant"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: __("Assistant"),
    single_column: true,
  });
  const history = [];
  let busy = false;

  page.set_primary_action(__("What is open for me"), () => {
    ask(__("What is open for me?"));
  });
  page.set_secondary_action(__("Clear"), () => {
    history.length = 0;
    $body.empty();
    greet();
  });

  const $body = $(
    `<div class="assistant-chat"><div class="assistant-messages"></div>
      <div class="assistant-input-row">
        <textarea class="assistant-input form-control" rows="2"
          placeholder="${__("Ask what is open, or how to get through it")}"></textarea>
        <button class="btn btn-primary btn-sm btn-send">${__("Send")}</button>
      </div>
      <p class="text-muted assistant-hint">
        ${__("Read-only: the assistant answers from what you may see and changes nothing.")}
      </p>
    </div>`,
  );
  page.main.append($body);
  const $messages = $body.find(".assistant-messages");
  const $input = $body.find(".assistant-input");

  function greet() {
    $messages.append(
      `<div class="assistant-msg assistant-msg--bot">${__(
        "Ask me what is open for you. I read with your own permissions and link what I name.",
      )}</div>`,
    );
  }

  function escape_html(text) {
    return $("<div>")
      .text(text == null ? "" : String(text))
      .html();
  }

  function add_message(role, text, items) {
    const mine = role === "user";
    const lines = escape_html(text).replace(/\n/g, "<br>");
    let html = `<div class="assistant-msg assistant-msg--${
      mine ? "user" : "bot"
    }">${lines || (mine ? "…" : __("No answer came back."))}`;
    if (!mine && items && items.length) {
      html += `<div class="assistant-items">${items
        .map(
          (item) =>
            `<a class="assistant-item" href="${escape_html(item.link)}">${escape_html(
              item.doctype,
            )}: ${escape_html(item.name)}</a>`,
        )
        .join("")}</div>`;
    }
    $messages.append(html + "</div>");
    $messages.scrollTop($messages[0].scrollHeight);
  }

  function set_busy(state) {
    busy = state;
    $body.find(".btn-send").prop("disabled", state);
    $input.prop("disabled", state);
  }

  function ask(message) {
    if (busy) return;
    const text = (message || "").trim();
    if (!text) return;
    add_message("user", text);
    $input.val("");
    set_busy(true);
    frappe
      .call({
        method: "bi_finance.assistant.ask",
        args: { message: text, history: history },
        freeze: true,
        freeze_message: __("Reading what is open"),
      })
      .then((r) => {
        const answer = (r && r.message) || {};
        add_message("assistant", answer.text || "", answer.items || []);
        history.push({ role: "user", content: text });
        history.push({ role: "assistant", content: answer.text || "" });
        // the server keeps twelve turns; the page mirrors that so a long thread stays bounded
        while (history.length > 2 * 12) history.splice(0, 2);
      })
      .always(() => set_busy(false));
  }

  $body.find(".btn-send").on("click", () => ask($input.val()));
  $input.on("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask($input.val());
    }
  });

  greet();
};
