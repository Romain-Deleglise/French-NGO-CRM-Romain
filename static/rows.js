/* Rend utilisables les lignes de tableau cliquables.
 *
 * Dix listes utilisent `<tr onclick="window.location='…'">`. Ça marche à la
 * souris et seulement à la souris : impossible d'ouvrir un fil dans un nouvel
 * onglet (clic du milieu, Ctrl+clic), impossible d'y arriver au clavier, et un
 * lecteur d'écran n'y voit rien d'activable.
 *
 * Plutôt que de réécrire les dix gabarits, on réparé ici, une fois : l'URL est
 * lue dans l'attribut `onclick` déjà présent, et la ligne devient un lien à
 * part entière.
 */
(function () {
  var URL_IN_ONCLICK = /window\.location\s*=\s*'([^']+)'/;

  function target(row) {
    var code = row.getAttribute("onclick") || "";
    var found = URL_IN_ONCLICK.exec(code);
    return found ? found[1] : null;
  }

  var rows = document.querySelectorAll("tr[onclick]");
  Array.prototype.forEach.call(rows, function (row) {
    var url = target(row);
    if (!url) return;

    row.setAttribute("role", "link");
    row.setAttribute("tabindex", "0");

    // Entrée active la ligne, comme un lien. Espace est laissé au défilement.
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        window.location = url;
      }
    });

    // Ctrl+clic, Cmd+clic et clic du milieu ouvrent dans un nouvel onglet —
    // ce qu'on attend de n'importe quelle liste : garder la liste ouverte et
    // regarder une fiche à côté.
    row.addEventListener("click", function (event) {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.open(url, "_blank", "noopener");
      }
    }, true);
    row.addEventListener("auxclick", function (event) {
      if (event.button === 1) {
        event.preventDefault();
        window.open(url, "_blank", "noopener");
      }
    });
  });
})();
