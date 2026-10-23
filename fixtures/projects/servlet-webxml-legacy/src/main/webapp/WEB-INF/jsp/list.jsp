<%@ page contentType="text/html;charset=UTF-8" pageEncoding="UTF-8" %>
<%@ page import="javax.servlet.http.Cookie, java.util.List" %>
<%
    javax.servlet.http.HttpSession s = request.getSession();
    List<?> titles = (List<?>) request.getAttribute("titles");
    Cookie[] cookies = request.getCookies();
%>
<html>
<body>
<p>session <%= s.getId() %>, <%= cookies == null ? 0 : cookies.length %> cookie(s)</p>
<ul>
<% if (titles != null) { for (Object t : titles) { %><li><%= t %></li><% } } %>
</ul>
</body>
</html>
