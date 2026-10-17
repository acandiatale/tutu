package com.example.greeting;

/**
 * 인사말 값 객체.
 * 레거시 윈도우 개발 환경(MS949)에서 작성된 소스 - 인코딩을 바꾸면 한글이 깨집니다.
 */
public final class Greeting {

    /** 기본 인사말 */
    public static final String DEFAULT_MESSAGE = "안녕하세요";

    private final String name;
    private final String message;

    public Greeting(String name, String message) {
        this.name = name;
        this.message = message;
    }

    public String getName() {
        return name;
    }

    public String getMessage() {
        return message;
    }

    /** 예: "홍길동님, 안녕하세요!" */
    public String render() {
        return name + "님, " + message + "!";
    }
}
