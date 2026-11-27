<%@ page contentType="text/html; charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jsp" %>
<html><head><link rel="stylesheet" href="${pageContext.request.contextPath}/css/shop.css"/></head><body>
<a href="${pageContext.request.contextPath}/SH1/SH1A10/order-list.mvc">orders</a>
<a href="${pageContext.request.contextPath}/SH1/SH1A10/item-list.mvc">items</a>
<form action="${pageContext.request.contextPath}/SH1/SH1A10/orderDetail.mvc" method="post"></form>
<script src="${pageContext.request.contextPath}/js/order.js"></script>
<script>
  function reload(tran) { return "commonAjax.json?ServiceName=order-service&" + tran + "=1"; }
</script>
</body></html>
