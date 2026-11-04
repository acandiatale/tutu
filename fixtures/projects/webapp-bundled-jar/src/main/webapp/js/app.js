(function () {
    fetch('api/greeting?name=visitor')
        .then(function (r) { return r.text(); })
        .then(function (t) { document.getElementById('greeting').textContent = t; });
})();
