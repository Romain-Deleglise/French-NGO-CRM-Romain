/* Zone de dépôt de courriels.
 *
 * Le gabarit s'en passe : la zone est un <label> associé à un <input file>,
 * donc sans JavaScript elle reste un bouton de sélection de fichiers qui
 * fonctionne. Ce script n'ajoute que le glisser-déposer et le retour visuel.
 */
(function () {
  var zone = document.getElementById("dropzone");
  var input = document.getElementById("courriels");
  var list = document.getElementById("dropzone-files");
  if (!zone || !input || !list) return;

  // Le sélecteur natif ("Choose Files", dans la langue du navigateur) n'a plus
  // lieu d'être une fois le clic sur la zone opérationnel : c'est le <label>
  // qui l'ouvre. Il reste visible sans JavaScript, où il est le seul moyen.
  zone.classList.add("has-js");

  function describe(files) {
    if (!files || !files.length) { list.textContent = ""; return; }
    if (files.length === 1) { list.textContent = files[0].name; return; }
    list.textContent = files.length + " fichiers";
  }

  input.addEventListener("change", function () { describe(input.files); });

  // Sans ces quatre preventDefault, le navigateur quitte la page pour afficher
  // le fichier déposé — et le dépôt est perdu.
  ["dragenter", "dragover", "dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function (event) {
      event.preventDefault();
      event.stopPropagation();
    });
  });
  ["dragenter", "dragover"].forEach(function (name) {
    zone.addEventListener(name, function () { zone.classList.add("is-over"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function () { zone.classList.remove("is-over"); });
  });

  zone.addEventListener("drop", function (event) {
    var dropped = event.dataTransfer && event.dataTransfer.files;
    if (!dropped || !dropped.length) return;
    // On repasse par l'<input> : le formulaire reste un envoi HTML ordinaire,
    // sans requête asynchrone ni gestion d'erreur en double.
    input.files = dropped;
    describe(dropped);
  });
})();
