// Solves the page's small puzzle (see proof.py) before a question is sent:
// finds the number whose SHA-256, after the page's salt, matches the page's
// hash. It starts as soon as the page loads, so it is usually done before the
// question is typed. If someone asks sooner, the question waits for it.
// No cookies, nothing sent anywhere until the question itself.
(function () {
  var meta = document.querySelector('meta[name="proof"]');
  var forms = [].filter.call(document.forms, function (f) {
    return /\/ask$/.test(f.getAttribute("action") || "");
  });
  if (!meta || !forms.length || !(window.crypto && crypto.subtle && window.TextEncoder)) return;
  var p = JSON.parse(meta.getAttribute("content"));
  var answer = null;
  var waiting = null;

  function hex(buf) {
    return [].map.call(new Uint8Array(buf), function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }

  function fill(form) {
    var fields = { proof_salt: p.salt, proof_challenge: p.challenge,
                   proof_signature: p.signature, proof_number: String(answer) };
    Object.keys(fields).forEach(function (name) {
      var input = form.querySelector('input[name="' + name + '"]');
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        form.appendChild(input);
      }
      input.value = fields[name];
    });
  }

  async function solve() {
    var enc = new TextEncoder();
    var batch = 2000;                      // hashes in flight at once
    for (var start = 0; start <= p.max; start += batch) {
      var tries = [];
      for (var n = start; n < start + batch && n <= p.max; n++) {
        tries.push(crypto.subtle.digest("SHA-256", enc.encode(p.salt + n)));
      }
      var hashes = await Promise.all(tries);
      for (var i = 0; i < hashes.length; i++) {
        if (hex(hashes[i]) === p.challenge) {
          answer = start + i;
          forms.forEach(fill);
          if (waiting) waiting.submit();
          return;
        }
      }
    }
  }

  forms.forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (answer !== null) return;         // solved: send as normal
      e.preventDefault();                  // not yet: send it the moment it is
      waiting = form;
      var button = form.querySelector('button[type="submit"], button:not([type])');
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = meta.getAttribute("data-wait");
      }
    });
  });

  solve();
})();
