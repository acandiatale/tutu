<%@ page contentType="text/html; charset=UTF-8" pageEncoding="UTF-8" %>
<%@ page import="javax.servlet.http.HttpSession" %>
<% HttpSession s = request.getSession(false); %>
<html>
<head><link rel="stylesheet" href="<%= request.getContextPath() %>/css/app.css"/></head>
<body>
<p>Session: <%= s == null ? "none" : s.getId() %></p>
<pre><%= request.getAttribute("rows") %></pre>
</body>
</html>
