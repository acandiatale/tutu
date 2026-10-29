<%@ page contentType="text/html; charset=UTF-8" pageEncoding="UTF-8" %>
<%@ page import="javax.servlet.http.HttpSession" %>
<%
    HttpSession s = request.getSession(true);
    Integer visits = (Integer) s.getAttribute("visits");
    visits = visits == null ? 1 : visits + 1;
    s.setAttribute("visits", visits);
%>
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>ant-webapp</title>
  <link rel="stylesheet" href="css/site.css">
</head>
<body>
  <h1>ant-webapp</h1>
  <p>Visit number <%= visits %>. <a href="hello?name=ant">Say hello</a></p>
</body>
</html>
