/* Menu de navigation repliable sur téléphone.
 *
 * Treize entrées, dont huit en pastilles colorées : sur un écran de 390 px la
 * barre occupait 450 px de haut, soit plus d'un écran avant le moindre contenu.
 *
 * Amélioration progressive, et dans cet ordre précis : c'est CE script qui pose
 * `has-js` sur l'en-tête, et la règle CSS qui replie le menu est conditionnée à
 * cette classe. Sans JavaScript — ou si ce fichier ne se charge pas — la barre
 * reste donc déroulée comme avant, au lieu de devenir un menu qui ne s'ouvre
 * jamais.
 */
(function () {
  var header = document.querySelector(".site-header");
  var toggle = header && header.querySelector(".nav-toggle");
  var nav = header && header.querySelector("nav");
  if (!header || !toggle || !nav) return;

  header.classList.add("has-js");

  function setOpen(open) {
    header.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  toggle.addEventListener("click", function () {
    setOpen(!header.classList.contains("nav-open"));
  });

  // Échap referme : sur un téléphone le menu couvre la page, et il faut pouvoir
  // en sortir autrement qu'en visant le bouton.
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setOpen(false);
  });

  // Une navigation recharge la page et referme donc le menu d'elle-même ; rien
  // à faire pour les liens.
})();
