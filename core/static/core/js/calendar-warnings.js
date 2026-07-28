/*
 * Non-blocking "this date is on the school calendar" warning for the
 * three due-date pickers that have one: Homework due dates, Fee Notice
 * due dates, and Permission Slip response deadlines. Deliberately a
 * warning, never a block — schools sometimes do want a due date to
 * fall right after a break, and this shouldn't get in the way of a
 * deliberate choice (see the School Calendar section of the wiki).
 *
 * Expects:
 *   - window.NOTIPA_CLOSED_DAYS_URL set by an inline <script> on the
 *     page before this file loads (core.urls:calendar_closed_days_json)
 *   - one or more <input type="date" data-warn-closed-days> elements
 *     already in the DOM before this script tag runs
 *
 * Fails silently on any error (network, bad JSON, no active school) —
 * this is a small enhancement on top of a plain date input, not
 * something the form's usability should depend on.
 */
(function () {
  var url = window.NOTIPA_CLOSED_DAYS_URL;
  var inputs = document.querySelectorAll('input[data-warn-closed-days]');
  if (!url || inputs.length === 0) return;

  fetch(url, { headers: { 'Accept': 'application/json' } })
    .then(function (res) {
      return res.ok ? res.json() : { closed_days: [] };
    })
    .then(function (data) {
      wireInputs(data.closed_days || []);
    })
    .catch(function () {
      // No warning is a safe fallback — the date field still works.
    });

  function wireInputs(closedDays) {
    if (closedDays.length === 0) return;

    inputs.forEach(function (input) {
      var hint = document.createElement('div');
      hint.className = 'field__hint field__hint--warning';
      hint.style.display = 'none';
      input.insertAdjacentElement('afterend', hint);

      function check() {
        var value = input.value; // "YYYY-MM-DD" from a native date input
        if (!value) {
          hint.style.display = 'none';
          return;
        }
        // ISO date strings compare correctly as plain strings, so this
        // avoids any Date()/timezone parsing entirely.
        var match = closedDays.find(function (day) {
          return value >= day.start && value <= day.end;
        });
        if (match) {
          hint.textContent = '⚠ ' + match.label + ' — the school calendar marks this as a closed day. You can still save this date if that’s intentional.';
          hint.style.display = 'block';
        } else {
          hint.style.display = 'none';
        }
      }

      input.addEventListener('change', check);
      check(); // covers an already-filled-in value, e.g. editing
    });
  }
})();
