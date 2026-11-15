<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<%@ page import="javax.servlet.http.HttpSession" %>
<%
    HttpSession current = request.getSession(false);
    javax.servlet.http.Cookie[] cookies = request.getCookies();
%>
<html>
<head><title>fwapp</title></head>
<body>
<h1>fwapp</h1>
<p>session: <%= current == null ? "new" : current.getId() %></p>
<p>cookies: <%= cookies == null ? 0 : cookies.length %></p>
<a href="<%= request.getContextPath() %>/greeting?name=guest">greeting</a>
</body>
</html>
