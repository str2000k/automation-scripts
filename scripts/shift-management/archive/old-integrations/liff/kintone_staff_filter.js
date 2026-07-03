(function () {
  "use strict";

  var STAFF_MASTER_APP_ID = 213;
  var DROPDOWN_ID = "chillaxy-staff-filter";

  function fetchStaffList() {
    return kintone
      .api(kintone.api.url("/k/v1/records", true), "GET", {
        app: STAFF_MASTER_APP_ID,
        query: 'active in ("有効") order by staff_id asc',
        fields: ["staff_id", "name"],
      })
      .then(function (resp) {
        return resp.records.map(function (r) {
          return { staff_id: r.staff_id.value, name: r.name.value };
        });
      });
  }

  function renderDropdown(headerSpace, staffList, selectedStaffId) {
    var existing = document.getElementById(DROPDOWN_ID);
    if (existing) existing.parentNode.removeChild(existing);

    var wrapper = document.createElement("div");
    wrapper.id = DROPDOWN_ID;
    wrapper.style.cssText =
      "display:inline-flex; align-items:center; padding:6px 8px; " +
      "background:#f5f5f5; border-radius:4px; margin-bottom:8px;";

    var label = document.createElement("label");
    label.textContent = "\u{1F464} \u30B9\u30BF\u30C3\u30D5\uFF1A";
    label.style.cssText = "font-size:13px; margin-right:6px; color:#333;";

    var select = document.createElement("select");
    select.style.cssText =
      "padding:4px 24px 4px 8px; border:1px solid #ccc; border-radius:4px; " +
      "font-size:13px; cursor:pointer; background:white; min-width:120px;";

    var optAll = document.createElement("option");
    optAll.value = "";
    optAll.textContent = "\uFF08\u3059\u3079\u3066\uFF09";
    optAll.selected = !selectedStaffId;
    select.appendChild(optAll);

    staffList.forEach(function (staff) {
      var opt = document.createElement("option");
      opt.value = staff.staff_id;
      opt.textContent = staff.name;
      opt.selected = staff.staff_id === selectedStaffId;
      select.appendChild(opt);
    });

    select.addEventListener("change", function () {
      var val = select.value;
      var appId = kintone.app.getId();
      if (val) {
        window.location.href =
          "/k/" + appId + "/?query=staff_id%3D%22" + encodeURIComponent(val) + "%22";
      } else {
        window.location.href = "/k/" + appId + "/";
      }
    });

    wrapper.appendChild(label);
    wrapper.appendChild(select);
    headerSpace.insertBefore(wrapper, headerSpace.firstChild);
  }

  function getSelectedStaffIdFromQuery() {
    var decoded = decodeURIComponent(window.location.search);
    var match = decoded.match(/staff_id="([^"]+)"/);
    return match ? match[1] : "";
  }

  kintone.events.on("app.record.index.show", function (event) {
    var headerSpace = kintone.app.getHeaderMenuSpaceElement
      ? kintone.app.getHeaderMenuSpaceElement()
      : kintone.app.getHeaderSpaceElement();
    if (!headerSpace) return event;

    var selectedStaffId = getSelectedStaffIdFromQuery();
    fetchStaffList()
      .then(function (staffList) {
        renderDropdown(headerSpace, staffList, selectedStaffId);
      })
      .catch(function (err) {
        console.error("[chillaxy] \u30B9\u30BF\u30C3\u30D5\u4E00\u89A7\u306E\u53D6\u5F97\u306B\u5931\u6557:", err);
      });
    return event;
  });
})();
