function items() { return api.get('/api/item'); }
function byName(name) { return api.get('/api/item/find-by-name?name=' + name); }
function notices() { return api.get('/api/notice/select-notices'); }
