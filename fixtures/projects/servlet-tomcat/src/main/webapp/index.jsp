<%@ page contentType="text/html;charset=UTF-8" language="java" %>
<html>
<head><title>Notice board</title></head>
<body>
<h1>Notice board</h1>
<form method="post" action="<%= request.getContextPath() %>/hello">
    <input name="title" placeholder="title"/>
    <textarea name="body"></textarea>
    <button type="submit">Post</button>
</form>
<a href="<%= request.getContextPath() %>/hello?name=guest">list</a>
</body>
</html>
