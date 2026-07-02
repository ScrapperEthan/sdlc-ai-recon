package com.hsbc.hase.ingress.resource;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/ingress")
public class IngressResource {
    @GetMapping("/health")
    public String health() {
        return "OK";
    }
}
