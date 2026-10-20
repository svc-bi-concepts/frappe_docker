// The Assistant desk page: a chat over the read-only collectors, see assistant.py.
//
// The page holds only the conversation: the question and the past turns go to
// bi_finance.assistant.ask, and what comes back is text plus the documents to link. The OpenRouter
// key never reaches the browser; the server holds it and the request alone. "What is open for me"
// runs the same call with the fixed question, so the daily opener needs no typing.
//
// With actions switched on, an answer also carries actions[]: one card per proposal the model made. The card shows the
// level, the confidence and each check, and the buttons that level allows: Undo (level 0, done), Approve / Reject
// (level 1), a link to the AI Action form (level 2, approved there). The buttons call the whitelisted approve_action,
// reject_action and undo_action with the AI Action name alone; the server takes the user from the session.

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
        ${__("The assistant answers from what you may see. It changes something only as a proposal below, as far as Actions are switched on.")}
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

  function check_html(check) {
    return `<li class="assistant-check assistant-check--${
      check.passed ? "pass" : "fail"
    }">${check.passed ? "✓" : "✗"} ${escape_html(check.check)}${
      check.detail ? ": " + escape_html(check.detail) : ""
    }</li>`;
  }

  function status_html(card) {
    const level = card.level;
    if (card.state === "auto-done") {
      return `<span class="assistant-status">${__("done")}</span>${
        card.undoable
          ? ` <button class="btn btn-default btn-xs" data-act="undo_action">${__("Undo")}</button>`
          : ""
      }`;
    }
    if (card.state === "approved") {
      return `<span class="assistant-status">${__("done")}</span>`;
    }
    if (card.state === "approved-dry-run") {
      return `<span class="assistant-status">${__("dry run: nothing was booked")}</span><div class="assistant-dry">${escape_html(
        card.dry_run_result,
      )}</div>`;
    }
    if (card.state === "undone") {
      return `<span class="assistant-status">${__("undone")}</span>`;
    }
    if (card.state === "proposed" && level === "1") {
      return `<button class="btn btn-primary btn-xs" data-act="approve_action">${
        card.dry_run ? __("Approve (dry run)") : __("Approve")
      }</button> <button class="btn btn-default btn-xs" data-act="reject_action">${__("Reject")}</button>`;
    }
    if (card.state === "proposed" && level === "2") {
      return `<span class="assistant-status">${__(
        "needs a second step: open the AI Action to approve",
      )}</span> <a href="${escape_html(card.form)}">${escape_html(card.name)}</a>`;
    }
    // rejected and failed: the reason
    return `<span class="assistant-status assistant-status--no">${escape_html(
      card.state === "failed" ? __("failed") : __("not done"),
    )}</span> ${escape_html(card.message)}`;
  }

  function render_card(card) {
    const confidence = Math.round((Number(card.confidence) || 0) * 100);
    const target = card.target_name
      ? ` ${escape_html(card.target_doctype)} ${escape_html(card.target_name)}`
      : "";
    const $card = $(
      `<div class="assistant-card assistant-card--l${escape_html(card.level)}">
        <div class="assistant-card-head"><b>${escape_html(card.type)}</b>${target}
          <span class="assistant-badge">${__("Level")} ${escape_html(card.level)}</span>
          <span class="assistant-badge">${__("Confidence")} ${confidence}%</span></div>
        <ul class="assistant-checks">${(card.checks || []).map(check_html).join("")}</ul>
        <div class="assistant-card-foot">${status_html(card)}</div>
      </div>`,
    );
    // a refused click leaves the card as it was; the reason is shown under it (rejected and failed print it already)
    if (
      !card.ok &&
      card.message &&
      !["rejected", "failed"].includes(card.state)
    ) {
      $card
        .find(".assistant-card-foot")
        .append(`<div class="text-muted">${escape_html(card.message)}</div>`);
    }
    $card.find("[data-act]").on("click", function () {
      const method = $(this).data("act");
      $card.find("[data-act]").prop("disabled", true);
      frappe
        .call({
          method: "bi_finance.assistant." + method,
          args: { action: card.name },
          freeze: true,
        })
        .then((r) => {
          if (r && r.message) $card.replaceWith(render_card(r.message));
        })
        .fail(() => $card.find("[data-act]").prop("disabled", false));
    });
    return $card;
  }

  function add_cards(cards) {
    (cards || []).forEach((card) => $messages.append(render_card(card)));
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
        add_cards(answer.actions);
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
