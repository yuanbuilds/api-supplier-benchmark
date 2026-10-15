(() => {
  const prepareLinks = () => {
    document.querySelectorAll("a.table-link, .summary-actions a.source").forEach((link) => {
      link.target = "_blank";
      link.rel = "noopener";
    });
    const sourceLink = document.querySelector(".summary-actions .source");
    if (sourceLink && sourceLink.textContent !== "打开 Promptfoo 原始报告 ↗") {
      sourceLink.textContent = "打开 Promptfoo 原始报告 ↗";
    }
  };

  prepareLinks();
  // Filtering rebuilds table rows, so mark any newly rendered anchor as native too.
  new MutationObserver(prepareLinks).observe(document.body, { childList: true, subtree: true });

  // `.drawer-layer` defines display:flex, which overrides the browser's default
  // `[hidden] { display:none }` rule in a self-contained local report.
  const drawer = document.querySelector("#drawer");
  if (drawer) {
    const syncDrawerVisibility = () => {
      drawer.style.display = drawer.hidden ? "none" : "flex";
    };
    syncDrawerVisibility();
    new MutationObserver(syncDrawerVisibility).observe(drawer, { attributes: true, attributeFilter: ["hidden"] });
  }
})();
