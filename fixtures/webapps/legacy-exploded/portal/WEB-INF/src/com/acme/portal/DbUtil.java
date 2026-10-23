package com.acme.portal;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;

import javax.naming.InitialContext;
import javax.sql.DataSource;

/** 데이터베이스 유틸리티 */
public final class DbUtil {
    private DbUtil() {
    }

    public static boolean check(String id) {
        try {
            DataSource ds = (DataSource) new InitialContext().lookup("java:comp/env/jdbc/PortalDS");
            Connection c = ds.getConnection();
            try {
                PreparedStatement ps = c.prepareStatement("select 1 from users where id = ?");
                ps.setString(1, id);
                ResultSet rs = ps.executeQuery();
                return rs.next();
            } finally {
                c.close();
            }
        } catch (Exception e) {
            System.out.println("사용자 확인 실패: " + e);
            return false;
        }
    }
}
