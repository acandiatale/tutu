package com.example.fw.core;

/** A result envelope of the stand-in framework core. */
public final class FwResult<T> {

    private final String code;
    private final T data;

    private FwResult(String code, T data) {
        this.code = code;
        this.data = data;
    }

    public static <T> FwResult<T> ok(T data) {
        return new FwResult<>("OK", data);
    }

    public static <T> FwResult<T> fail(String code) {
        return new FwResult<>(code, null);
    }

    public String getCode() {
        return code;
    }

    public T getData() {
        return data;
    }

    public boolean isOk() {
        return "OK".equals(code);
    }
}
