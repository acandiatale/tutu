package com.example.orders.json;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;

import org.springframework.boot.jackson.JsonComponent;

import com.fasterxml.jackson.core.JsonGenerator;
import com.fasterxml.jackson.databind.JsonSerializer;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializerProvider;

/**
 * Serializes every BigDecimal with two fraction digits. Jackson 2 / @JsonComponent (Boot 4 moves to Jackson 3,
 * tools.jackson packages and @JacksonComponent).
 */
@JsonComponent
public class MoneyJsonComponent {

    public static class Serializer extends JsonSerializer<BigDecimal> {
        @Override
        public void serialize(BigDecimal value, JsonGenerator gen, SerializerProvider serializers) throws IOException {
            gen.writeString(value.setScale(2, RoundingMode.HALF_UP).toPlainString());
        }

        @Override
        public Class<BigDecimal> handledType() {
            return BigDecimal.class;
        }
    }

    public static String debugJson(ObjectMapper mapper, Object value) {
        try {
            return mapper.writerWithDefaultPrettyPrinter().writeValueAsString(value);
        } catch (IOException e) {
            return String.valueOf(value);
        }
    }
}
